"""Read-only production readiness checks for the NFL shadow pipeline.

Unlike the scheduler, this command never constructs a ``ShadowStore`` (whose
constructor creates tables), reserves capture slots, or persists an execution.
Provider I/O is opt-in and limited to The Odds API's read-only endpoints.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError

from app.config import settings
from app.current_features import validate_current_rows
from app.identities import canonical_event_identity
from app.modeling.benchmark import MARKETS
from app.production_pipeline import CurrentFeatureCache, ProductionModels, load_production_models
from app.providers.the_odds_api import TheOddsApiProvider, _parse_datetime
from app.scheduler import sanitized_error
from app.shadow_selection import (FEATURE_MAX_AGE_SECONDS, FROZEN_ELIGIBLE_WINDOW,
                                  FROZEN_POLICY_NAME, FROZEN_POLICY_VERSION,
                                  MODEL_VERSION_ALLOWLIST, UNCERTAINTY_METHOD,
                                  UNCERTAINTY_VERSION)
from app.shadow_storage import (normalize_database_url, opportunity_decisions,
                                scheduler_executions)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Read-only NFL production readiness diagnostics")
    command.add_argument("--check-live-props", action="store_true",
                         help="opt in to minimal read-only The Odds API event/Hard Rock prop checks")
    return command


def _check(name: str, status: str, **details: Any) -> dict[str, Any]:
    return {"name": name, "status": status, **details}


def _artifact_provenance(models: ProductionModels) -> None:
    for market, scorer in models.scorers.items():
        artifact = scorer.artifact
        if artifact.get("canonical_market") != market:
            raise ValueError(f"artifact canonical_market does not match {market}")
        provenance = artifact.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError(f"artifact provenance is absent for {market}")
        required = {"built_at_utc", "training_seasons", "validation_calibration_season",
                    "feature_schema", "calibration_methodology", "calibration_version"}
        if missing := required - provenance.keys():
            raise ValueError(f"artifact provenance is incomplete for {market}: {sorted(missing)}")
        if list(provenance["feature_schema"]) != list(artifact["features"]):
            raise ValueError(f"artifact provenance feature schema differs for {market}")
        pd.to_datetime(provenance["built_at_utc"], utc=True, errors="raise")


def _configuration_check(live: bool) -> dict[str, Any]:
    configured = {
        "database_url": bool(settings.database_url),
        "football_current_feature_path": bool(settings.football_current_feature_path),
        "football_artifact_player_pass_yds": bool(settings.football_artifact_player_pass_yds),
        "football_artifact_player_reception_yds": bool(settings.football_artifact_player_reception_yds),
        "football_artifact_player_receptions": bool(settings.football_artifact_player_receptions),
        "the_odds_api_key": bool(settings.the_odds_api_key),
    }
    invalid = [name for name, value in configured.items() if not value]
    if settings.football_current_feature_max_age_seconds < 0:
        invalid.append("football_current_feature_max_age_seconds")
    # Keys and URLs are intentionally represented only as booleans.
    return _check("production_configuration", "failed" if invalid else "passed",
                  configured=configured, live_prop_check_requested=live,
                  reasons=[f"{name} is not configured or invalid" for name in invalid])


def _database_check(engine_factory: Callable[..., Any]) -> dict[str, Any]:
    engine = engine_factory(normalize_database_url(settings.database_url))
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
            try:
                decision_count = connection.execute(select(text("count(*)")).select_from(
                    opportunity_decisions)).scalar_one()
            except (ProgrammingError, OperationalError, DBAPIError) as exc:
                return _check("database_read_only", "failed", connectivity="passed",
                              decision_table_available=False, decision_count=None,
                              latest_scheduler_execution=None,
                              reasons=[f"shadow_opportunity_decisions is unavailable: {type(exc).__name__}"])
            latest = None
            try:
                row = connection.execute(select(scheduler_executions).order_by(
                    scheduler_executions.c.started_at_utc.desc()).limit(1)).first()
                if row:
                    item = dict(row._mapping)
                    detail = item.get("details") or {}
                    latest = {"execution_id": item["execution_id"],
                              "started_at_utc": item["started_at_utc"],
                              "ended_at_utc": item.get("ended_at_utc"),
                              "quota_before": detail.get("quota_before"),
                              "quota_after": detail.get("quota_after"),
                              "actual_credits_consumed": detail.get("actual_credits_consumed"),
                              "http_request_count": detail.get("http_request_count")}
            except (ProgrammingError, OperationalError, DBAPIError):
                # Connectivity is healthy, but a brand-new database can have no
                # scheduler table yet. Preflight must not create it.
                return _check("database_read_only", "warning", connectivity="passed",
                              decision_table_available=True, decision_count=decision_count,
                              latest_scheduler_execution=None,
                              reasons=["scheduler execution history is unavailable; no schema was created"])
        return _check("database_read_only", "passed", connectivity="passed",
                      decision_table_available=True, decision_count=decision_count,
                      latest_scheduler_execution=latest,
                      reasons=[] if latest else ["no prior scheduler execution found"])
    finally:
        engine.dispose()


def _kickoff_crosscheck(events: list[dict[str, Any]], future: pd.DataFrame,
                        tolerance_seconds: float = 300) -> dict[str, Any]:
    """Compare matched nflverse feature games with provider UTC commence times."""
    provider_games: dict[str, datetime] = {}
    for event in events:
        identity = canonical_event_identity(event.get("away_team"), event.get("home_team"))
        kickoff = _parse_datetime(event.get("commence_time"))
        if identity and kickoff:
            provider_games[identity] = kickoff.astimezone(timezone.utc)

    feature_games = (future[["_event", "_kickoff"]].drop_duplicates()
                     if not future.empty else pd.DataFrame(columns=["_event", "_kickoff"]))
    comparisons = []
    for row in feature_games.to_dict("records"):
        if row["_event"] not in provider_games:
            continue
        feature_kickoff = pd.Timestamp(row["_kickoff"]).to_pydatetime().astimezone(timezone.utc)
        provider_kickoff = provider_games[row["_event"]]
        delta = abs((feature_kickoff - provider_kickoff).total_seconds())
        comparisons.append({"event_identity": row["_event"],
                            "current_feature_kickoff_utc": feature_kickoff.isoformat(),
                            "provider_commence_time_utc": provider_kickoff.isoformat(),
                            "absolute_difference_seconds": delta})
    mismatches = [item for item in comparisons
                  if item["absolute_difference_seconds"] > tolerance_seconds]
    if mismatches:
        status, reasons = "failed", ["matched nflverse/current-feature and provider kickoffs materially differ"]
    elif not comparisons:
        status, reasons = "warning", ["no provider events could be matched to current-feature team pairs"]
    else:
        status, reasons = "passed", []
    return _check("live_event_kickoff_crosscheck", status,
                  tolerance_seconds=tolerance_seconds, matched_event_count=len(comparisons),
                  material_mismatch_count=len(mismatches), comparisons=comparisons, reasons=reasons)


async def _live_props(provider_factory: Callable[..., Any], now: datetime,
                      future: pd.DataFrame) -> tuple[dict[str, Any], dict[str, Any]]:
    provider = provider_factory(bookmakers=(settings.the_odds_api_target_bookmaker,))
    events = await provider.discover_events()
    end = now.timestamp() + settings.the_odds_api_lookahead_days * 86400
    upcoming = [event for event in events if (kickoff := _parse_datetime(event.get("commence_time")))
                and now.timestamp() < kickoff.timestamp() <= end]
    rows, checked = [], 0
    found: set[str] = set()
    for event in upcoming:
        event_rows = await provider.fetch_event_player_props(event["id"], observed_at=now)
        checked += 1
        rows.extend(event_rows)
        found.update(row.market_type.value for row in event_rows
                     if row.source == settings.the_odds_api_target_bookmaker)
        if found == set(MARKETS):
            break
    counts = {market: sum(row.market_type.value == market and
                          row.source == settings.the_odds_api_target_bookmaker for row in rows)
              for market in MARKETS}
    events_with_props = len({row.game_id for row in rows
                             if row.source == settings.the_odds_api_target_bookmaker})
    ok = bool(upcoming) and found == set(MARKETS)
    props = _check("live_hard_rock_props", "passed" if ok else "failed",
                  upcoming_event_count=len(upcoming), events_checked=checked,
                  bookmaker=settings.the_odds_api_target_bookmaker,
                  bookmaker_count=int(bool(rows)), events_with_hard_rock_props=events_with_props,
                  market_row_counts=counts, markets_available=sorted(found),
                  quota_usage=dict(provider.usage),
                  reasons=[] if ok else ["upcoming Hard Rock props were not available for all canonical markets"])
    return props, _kickoff_crosscheck(upcoming, future)


async def run_preflight(*, check_live_props: bool = False, now: datetime | None = None,
                        model_loader: Callable[[], ProductionModels] = load_production_models,
                        feature_cache_factory: Callable[..., CurrentFeatureCache] = CurrentFeatureCache,
                        engine_factory: Callable[..., Any] = create_engine,
                        odds_provider_factory: Callable[..., Any] = TheOddsApiProvider) -> tuple[dict, int]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    report: dict[str, Any] = {"generated_at_utc": current.isoformat(), "read_only": True,
        "live_prop_check_requested": check_live_props,
        "policy": {"name": FROZEN_POLICY_NAME, "version": FROZEN_POLICY_VERSION,
            "effective_eligible_window": settings.shadow_selection_eligible_window,
            "required_eligible_window": FROZEN_ELIGIBLE_WINDOW,
            "nominal_unit": settings.shadow_nominal_unit,
            "supported_markets": list(MARKETS),
            "provider_max_age_seconds": settings.scheduler_provider_stale_seconds,
            "feature_max_age_seconds": FEATURE_MAX_AGE_SECONDS,
            "broader_cache_max_age_seconds": settings.football_current_feature_max_age_seconds,
            "uncertainty_method": UNCERTAINTY_METHOD, "uncertainty_version": UNCERTAINTY_VERSION},
        "checks": []}
    policy_reasons = []
    if settings.shadow_selection_policy_name != FROZEN_POLICY_NAME:
        policy_reasons.append("configured policy name does not match frozen V2.0")
    if settings.shadow_selection_policy_version != FROZEN_POLICY_VERSION:
        policy_reasons.append("configured policy version does not match frozen V2.0")
    if settings.shadow_selection_eligible_window != FROZEN_ELIGIBLE_WINDOW:
        policy_reasons.append("effective eligible window is not the frozen 90m window")
    if settings.shadow_nominal_unit != 10.0:
        policy_reasons.append("nominal unit is not exactly 10.00")
    if settings.scheduler_provider_stale_seconds != 300:
        policy_reasons.append("provider freshness does not match frozen V2.0")
    report["checks"].append(_check("v2_policy_configuration",
        "failed" if policy_reasons else "passed", reasons=policy_reasons))
    report["checks"].append(_configuration_check(check_live_props))
    models = None
    try:
        models = model_loader()
        _artifact_provenance(models)
        artifacts = {market: {"path": data["artifact_path"], "sha256": data["artifact_sha256"],
            "model_version": data["model_version"], "expected_model_version": MODEL_VERSION_ALLOWLIST[market],
            "allowlist_match": data["artifact_sha256"] == MODEL_VERSION_ALLOWLIST[market] and
                               data["model_version"] == MODEL_VERSION_ALLOWLIST[market],
            "uncertainty_method": models.scorers[market].METHOD,
            "uncertainty_version": models.scorers[market].VERSION,
            "required_feature_count": len(data["required_features"])}
            for market, data in models.provenance.items()}
        mismatches = [market for market, data in artifacts.items() if not data["allowlist_match"] or
                      data["uncertainty_method"] != UNCERTAINTY_METHOD or
                      data["uncertainty_version"] != UNCERTAINTY_VERSION]
        report["checks"].append(_check("production_model_artifacts",
            "failed" if mismatches else "passed", count=len(models.scorers), artifacts=artifacts,
            reasons=[f"artifact identity or uncertainty contract mismatch: {market}" for market in mismatches]))
    except Exception as exc:
        report["checks"].append(_check("production_model_artifacts", "failed",
                                       reasons=[sanitized_error(exc)]))

    future = pd.DataFrame()
    if models is not None:
        try:
            if not settings.football_current_feature_path:
                raise RuntimeError("FOOTBALL_CURRENT_FEATURE_PATH is not configured")
            cache = feature_cache_factory(settings.football_current_feature_path)
            validate_current_rows(cache.rows.drop(columns=["_kickoff", "_built", "_asof", "_player", "_event"]), models)
            future = cache.rows[cache.rows._kickoff > current].copy()
            if future.empty:
                raise ValueError("no upcoming NFL feature rows")
            # Freshness is fail-closed against the oldest represented build;
            # one newer row must not conceal stale rows in a mixed cache.
            built_min = future._built.min().to_pydatetime()
            built_max = future._built.max().to_pydatetime()
            cutoff = future._asof.max().to_pydatetime()
            maximum_age = (current - built_min).total_seconds()
            minimum_age = (current - built_max).total_seconds()
            maximum = settings.football_current_feature_max_age_seconds
            v2_fresh = minimum_age >= 0 and maximum_age <= FEATURE_MAX_AGE_SECONDS
            games = (future[["_event", "away_team", "home_team", "_kickoff"]].drop_duplicates()
                     .sort_values("_kickoff"))
            by_game_market = future.groupby(["_event", "canonical_market"]).size()
            freshness_reasons = ([] if v2_fresh else
                [f"feature rows do not satisfy V2 freshness (oldest={maximum_age:.1f}s, "
                 f"newest={minimum_age:.1f}s, limit={FEATURE_MAX_AGE_SECONDS:.0f}s)"])
            report["checks"].append(_check("current_feature_cache", "passed" if v2_fresh else "failed",
                path=str(Path(settings.football_current_feature_path).expanduser().resolve()),
                feature_file_exists=Path(settings.football_current_feature_path).expanduser().is_file(),
                feature_built_at_utc_min=built_min, feature_built_at_utc_max=built_max,
                feature_age_seconds_min=minimum_age, feature_age_seconds_max=maximum_age,
                v2_feature_max_age_seconds=FEATURE_MAX_AGE_SECONDS, v2_freshness_satisfied=v2_fresh,
                source_cutoff=cutoff,
                configured_maximum_age_seconds=maximum,
                upcoming_games=[{"event_identity": row["_event"], "away_team": row["away_team"],
                                 "home_team": row["home_team"], "kickoff_utc": row["_kickoff"]}
                                for row in games.to_dict("records")],
                row_counts_by_game_and_market={event: {market: int(by_game_market.get((event, market), 0))
                    for market in MARKETS} for event in games._event},
                unique_eligible_players_by_market={market: int(group.player_id.nunique())
                    for market, group in future.groupby("canonical_market")},
                exact_identity_duplicates=int(future.duplicated(["player_id", "_kickoff", "canonical_market"]).sum()),
                reasons=freshness_reasons))
        except Exception as exc:
            configured_path = Path(settings.football_current_feature_path).expanduser() \
                if settings.football_current_feature_path else None
            report["checks"].append(_check("current_feature_cache", "failed",
                path=str(configured_path.resolve()) if configured_path else None,
                feature_file_exists=bool(configured_path and configured_path.is_file()),
                v2_feature_max_age_seconds=FEATURE_MAX_AGE_SECONDS,
                v2_freshness_satisfied=False, reasons=[sanitized_error(exc)]))

    if models is not None and not future.empty:
        try:
            scores = {}
            for market in MARKETS:
                row = future[future.canonical_market == market]
                if row.empty:
                    raise ValueError(f"no upcoming representative row for {market}")
                item = row.iloc[0]
                scorer = models.scorers[market]
                frame = pd.DataFrame([{f: item[f] for f in scorer.artifact["features"]}])
                point = float(scorer.artifact["pipeline"].predict(frame)[0]) + float(
                    scorer.artifact.get("additive_bias_correction", 0.0))
                score = scorer.score(frame, point, item._built.to_pydatetime())
                values = (score.point_prediction, score.over_probability, score.under_probability,
                          score.uncertainty_scale)
                if not all(math.isfinite(value) for value in values) or not (0 <= score.over_probability <= 1) \
                        or not math.isclose(score.over_probability + score.under_probability, 1.0):
                    raise ValueError(f"invalid prediction or uncertainty output for {market}")
                scores[market] = {"player_id": str(item.player_id), "event_identity": item._event,
                                  "point_prediction": score.point_prediction,
                                  "over_probability": score.over_probability,
                                  "under_probability": score.under_probability,
                                  "uncertainty_method": score.uncertainty_method,
                                  "uncertainty_version": score.uncertainty_version}
            report["checks"].append(_check("representative_model_scoring", "passed", scores=scores, reasons=[]))
        except Exception as exc:
            report["checks"].append(_check("representative_model_scoring", "failed", reasons=[sanitized_error(exc)]))

    try:
        report["checks"].append(_database_check(engine_factory))
    except Exception as exc:
        report["checks"].append(_check("database_read_only", "failed", reasons=[sanitized_error(exc)]))

    if check_live_props:
        try:
            props, kickoff_crosscheck = await _live_props(odds_provider_factory, current, future)
            report["checks"].extend((props, kickoff_crosscheck))
        except Exception as exc:
            report["checks"].append(_check("live_hard_rock_props", "failed", reasons=[sanitized_error(exc)]))
            report["checks"].append(_check("live_event_kickoff_crosscheck", "failed",
                                           reasons=["live event kickoff comparison could not be completed"]))
    else:
        report["checks"].append(_check("live_hard_rock_props", "skipped",
                                       reasons=["use --check-live-props to opt in; no provider request was made"]))
    statuses = {item["status"] for item in report["checks"]}
    report["status"] = "failed" if "failed" in statuses else "warning" if "warning" in statuses else "ready"
    report["ready"] = report["status"] == "ready"
    report["v2_status"] = "READY" if report["ready"] else "NOT_READY"
    report["reasons"] = [reason for check in report["checks"] if check["status"] in {"failed", "warning"}
                         for reason in check.get("reasons", [])]
    return report, int(not report["ready"])


def main() -> int:
    args = parser().parse_args()
    report, code = asyncio.run(run_preflight(check_live_props=args.check_live_props))
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
