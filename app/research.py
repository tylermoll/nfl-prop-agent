"""Read-only, sanitized views over persisted shadow-research data."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, create_engine, or_, select

from app.config import settings
from app.research_dashboard import DASHBOARD_HTML
from app.pregame_context import evaluate_context
from app.shadow_storage import (normalize_database_url, observations, pregame_context_snapshots,
                                opportunity_decisions, scheduler_executions, scheduler_slots,
                                settlements, selection_journal)
from app.research_export import calibration_table, edge_bucket, performance_summary

router = APIRouter(prefix="/research", tags=["research"])
_SECRET_KEY = re.compile(r"(?i)(secret|password|credential|authorization|api[_-]?key|private[_-]?key|raw[_-]?payload)")
_URL = re.compile(r"(?i)https?://\S+")
_INLINE_SECRET = re.compile(r"(?i)(api[_-]?key|token|authorization|password)=?[^&\s]*")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def sanitize(value: Any) -> Any:
    """Recursively redact URL/credential material from legacy JSON fields."""
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return _URL.sub("[URL REDACTED]", _INLINE_SECRET.sub(r"\1=[REDACTED]", value))[:2000]
    return value


class ResearchRepository:
    """Query-only repository. It deliberately does not create tables or expose writes."""

    def __init__(self, database_url: str | None = None, *, engine=None):
        self.engine = engine or create_engine(normalize_database_url(database_url or settings.database_url))

    def executions(self, limit: int, offset: int) -> list[dict]:
        query = select(scheduler_executions).order_by(
            scheduler_executions.c.started_at_utc.desc(), scheduler_executions.c.execution_id.desc()
        ).limit(limit).offset(offset)
        with self.engine.connect() as conn:
            return [execution_view(dict(row._mapping)) for row in conn.execute(query)]

    def observation_query(self, filters: dict[str, Any]):
        query = select(observations, *[c for c in settlements.c if c.name not in {"observation_id", "line", "side"}]).outerjoin(
            settlements, observations.c.observation_id == settlements.c.observation_id
        )
        clauses = []
        exact = {"market": observations.c.canonical_market, "player": observations.c.player_name,
                 "side": observations.c.side}
        for key, column in exact.items():
            if filters.get(key) is not None:
                clauses.append(column == filters[key])
        if filters.get("event") is not None:
            value = filters["event"]
            clauses.append(or_(observations.c.game_id == value, observations.c.team == value,
                               observations.c.opponent == value))
        if filters.get("start") is not None: clauses.append(observations.c.observed_at_utc >= filters["start"])
        if filters.get("end") is not None: clauses.append(observations.c.observed_at_utc <= filters["end"])
        if filters.get("settled") is True: clauses.append(settlements.c.observation_id.is_not(None))
        if filters.get("settled") is False: clauses.append(settlements.c.observation_id.is_(None))
        if filters.get("edge_tier") is not None: clauses.append(observations.c.edge_bucket == filters["edge_tier"])
        if clauses: query = query.where(and_(*clauses))
        return query

    def observations(self, filters: dict[str, Any], limit: int, offset: int) -> list[dict]:
        query = self.observation_query(filters).order_by(
            observations.c.observed_at_utc.desc(), observations.c.observation_id.desc())
        # Capture slot is embedded in immutable JSON; compare it exactly in SQL.
        if filters.get("capture_window") is not None:
            query = query.where(observations.c.context["capture_slot"].as_string() == filters["capture_window"])
        with self.engine.connect() as conn:
            rows = list(conn.execute(query.limit(limit).offset(offset)))
            views = [observation_view(dict(row._mapping), detail=False) for row in rows]
            return [self._with_context(conn, view) for view in views]

    def observation(self, observation_id: str) -> dict | None:
        query = self.observation_query({}).where(observations.c.observation_id == observation_id)
        with self.engine.connect() as conn:
            row = conn.execute(query).first()
            return self._with_context(conn, observation_view(dict(row._mapping), detail=True)) if row else None

    def _with_context(self, conn, view: dict, as_of: datetime | None = None) -> dict:
        """Attach the newest append-only snapshot known by ``as_of``."""
        cutoff = _utc(as_of) or datetime.now(timezone.utc)
        row = conn.execute(select(pregame_context_snapshots).where(
            pregame_context_snapshots.c.observation_id == view["observation_id"],
            pregame_context_snapshots.c.as_of_utc <= cutoff).order_by(
                pregame_context_snapshots.c.as_of_utc.desc(),
                pregame_context_snapshots.c.context_id.desc()).limit(1)).first()
        snapshot = sanitize(dict(row._mapping)) if row else None
        # evaluate_context returns a new structure and never receives a database row.
        return {**view, "context_overlay": evaluate_context(view, snapshot, now=cutoff)}

    def context(self, observation_id: str, as_of: datetime | None = None) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(self.observation_query({}).where(
                observations.c.observation_id == observation_id)).first()
            if row is None:
                return None
            return self._with_context(conn, observation_view(dict(row._mapping), detail=True), as_of)["context_overlay"]

    def timeline(self, observation_id: str) -> list[dict] | None:
        """Fetch the complete pregame series for one observation's market identity."""
        with self.engine.connect() as conn:
            anchor = conn.execute(select(observations).where(
                observations.c.observation_id == observation_id)).first()
            if anchor is None:
                return None
            row = anchor._mapping
            identity = [observations.c.game_id == row["game_id"],
                        observations.c.player_name == row["player_name"],
                        observations.c.canonical_market == row["canonical_market"],
                        observations.c.side == row["side"],
                        observations.c.observed_at_utc < observations.c.kickoff_utc]
            if row["player_id"] is None:
                identity.append(observations.c.player_id.is_(None))
            else:
                identity.append(observations.c.player_id == row["player_id"])
            rows = conn.execute(select(observations).where(and_(*identity)).order_by(
                observations.c.observed_at_utc, observations.c.observation_id)).all()
        return [observation_view(dict(item._mapping), detail=False) for item in rows]

    def historical_performance(self) -> dict:
        joined = observations.join(settlements, observations.c.observation_id == settlements.c.observation_id)
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(observations,
                *[c for c in settlements.c if c.name not in {"observation_id", "line", "side"}]).select_from(joined))]
            journals = [dict(r._mapping) for r in conn.execute(select(selection_journal).order_by(
                selection_journal.c.selected_at_utc, selection_journal.c.journal_id))]
        enriched = []
        for source in rows:
            ref, flags = source.get("reference_context") or {}, source.get("confirmation_flags") or {}
            enriched.append(dict(source, model_edge_bucket=edge_bucket(source["raw_probability_edge_pp"]),
                capture_window=(source.get("context") or {}).get("capture_slot"),
                confirmation_status="confirmed" if flags.get("both_agree") else "conflicted" if flags.get("both_disagree") else "available",
                reference_book_coverage=ref.get("reference_book_count"),
                evidence_quality=(source.get("context") or {}).get("evidence_quality")))
        latest = {item["observation_id"]: item for item in journals}
        selected = [row for row in enriched if latest.get(row["observation_id"], {}).get("selected")]
        dimensions = ("model_edge_bucket", "canonical_market", "side", "capture_window",
                      "confirmation_status", "reference_book_coverage", "evidence_quality")
        return {"by_dimensions": performance_summary(enriched, dimensions),
                "by_edge_bucket": performance_summary(enriched, ("model_edge_bucket",)),
                "by_market": performance_summary(enriched, ("canonical_market",)),
                "by_side": performance_summary(enriched, ("side",)),
                "by_capture_window": performance_summary(enriched, ("capture_window",)),
                "by_confirmation_status": performance_summary(enriched, ("confirmation_status",)),
                "by_reference_coverage": performance_summary(enriched, ("reference_book_coverage",)),
                "by_evidence_quality": performance_summary(enriched, ("evidence_quality",)),
                "calibration": calibration_table(enriched),
                "selection_comparison": None if len(selected) < 10 else {
                    "selected": performance_summary(selected)[0], "model_only": performance_summary(enriched)[0]},
                "selection_note": "At least 10 settled journal selections are required." if len(selected) < 10 else None}


@lru_cache
def get_repository() -> ResearchRepository:
    return ResearchRepository()


def _count(detail: dict, key: str) -> int:
    value = detail.get(key, [])
    return len(value) if isinstance(value, list) else int(value or 0)


def execution_view(row: dict) -> dict:
    detail = sanitize(row.get("details") or {})
    return {"execution_id": row["execution_id"], "started_at_utc": _utc(row["started_at_utc"]),
            "ended_at_utc": _utc(row.get("ended_at_utc")), "dry_run": bool(detail.get("dry_run", False)),
            **{f"{key}_count": _count(detail, key) for key in ("due", "selected", "completed", "deferred", "failed")},
            "estimated_credits": detail.get("estimated_credits", 0),
            "actual_credits_consumed": detail.get("actual_credits_consumed", 0),
            "quota_before": detail.get("quota_before"), "quota_after": detail.get("quota_after"),
            "http_request_count": detail.get("http_request_count", 0), "errors": detail.get("errors", []),
            # Historically this list was persisted but discarded here, which
            # made aggregate failures impossible to diagnose from /research.
            "failed_slots": detail.get("failed", []) if isinstance(detail.get("failed", []), list) else []}


def observation_view(row: dict, *, detail: bool) -> dict:
    ref, kalshi = sanitize(row.get("reference_context") or {}), sanitize(row.get("kalshi_context") or {})
    fresh, context = sanitize(row.get("freshness") or {}), sanitize(row.get("context") or {})
    bid, ask = kalshi.get("yes_bid"), kalshi.get("yes_ask")
    midpoint = kalshi.get("midpoint")
    spread = (ask - bid) if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) else None
    settled = row.get("settled_at_utc") is not None
    model_over = row["model_probability"] if row["side"] == "over" else 1 - row["model_probability"]
    result = {"observation_id": row["observation_id"], "captured_at_utc": _utc(row["observed_at_utc"]),
        "capture_window": context.get("capture_slot"), "kickoff_utc": _utc(row["kickoff_utc"]),
        "event": {"game_id": row["game_id"], "matchup": " at ".join(x for x in (row.get("team"), row.get("opponent")) if x),
                  "team": row.get("team"), "opponent": row.get("opponent")},
        "player": {"id": row.get("player_id"), "name": row["player_name"]},
        "canonical_market": row["canonical_market"], "side": row["side"], "hard_rock_line": row["line"],
        "hard_rock_american_price": row["hard_rock_offered_odds"],
        "offered_price_break_even_probability": row["offered_price_break_even_probability"],
        "model_point_prediction": row.get("point_prediction"), "model_probability": row["model_probability"],
        "model_over_probability": model_over, "model_under_probability": 1 - model_over,
        "probability_edge_pp": row["raw_probability_edge_pp"], "edge_tier": row["edge_bucket"],
        "reference_book_coverage": ref.get("reference_book_count", len(ref.get("books", []))),
        "reference_median_line": ref.get("median_line"),
        "same_threshold_reference_probability": ref.get("exact_threshold_over_no_vig_probability"),
        "kalshi": {"yes_bid": bid, "yes_ask": ask, "midpoint": midpoint, "spread": spread,
                   "volume": kalshi.get("volume"), "open_interest": kalshi.get("open_interest")},
        "model_version": row["model_version"], "feature_built_at_utc": _utc(row["feature_built_at_utc"]),
        "feature_freshness": {"age_seconds": fresh.get("model_feature_age_seconds"), "stale": fresh.get("model_stale")},
        "provider_freshness": {"observed_at_utc": fresh.get("provider_observed_at_utc"),
                               "updated_at_utc": fresh.get("provider_updated_at_utc"),
                               "age_seconds": fresh.get("provider_age_seconds"), "stale": fresh.get("provider_stale")},
        "confirmation_flags": sanitize(row.get("confirmation_flags") or {}), "settlement_status": "settled" if settled else "unsettled",
        "settlement": ({"settled_at_utc": _utc(row["settled_at_utc"]), "actual_value": row.get("actual_value"),
                        "result": row.get("result"), "paper_profit_loss_per_dollar": row.get("profit_loss_per_dollar"),
                        "paper_fixed_unit": row.get("fixed_unit"), "paper_profit_loss": row.get("fixed_unit_profit_loss"),
                        "american_odds_payout_used": (row.get("american_odds") if row.get("american_odds") is not None
                                                       else row.get("hard_rock_offered_odds"))}
                       if settled else None)}
    if detail:
        result["provenance"] = {"source_observation_ids": sanitize(row.get("source_observation_ids") or {}),
                                "reference_context": ref, "kalshi_context": kalshi, "freshness": fresh,
                                "context": context, "uncertainty": {"method": row["uncertainty_method"],
                                "version": row["uncertainty_version"], "residual_bucket": row.get("residual_bucket"),
                                "scale": row.get("uncertainty_scale")}, "research_config": sanitize(row.get("research_config") or {}),
                                "settlement_result_source_id": row.get("result_source_id")}
    return result


def filter_params(market: str | None = None, player: str | None = None, event: str | None = None,
                  side: str | None = None, capture_window: str | None = None, edge_tier: str | None = None,
                  settled: bool | None = None, start: datetime | None = None, end: datetime | None = None) -> dict:
    if start and end and start > end: raise HTTPException(422, "start must not be after end")
    return locals()


def _strategy_performance(rows: list[dict], selected_count: int) -> dict:
    """Aggregate only prospectively journaled selections.

    ``rows`` must come from the selection -> observation -> settlement inner
    join.  Keeping that boundary at the query makes it impossible for the two
    raw sides captured for a market to silently become a betting record.
    """
    settled_rows = [row for row in rows if row.get("settled_at_utc") is not None]
    if not settled_rows:
        return {"available": False, "status": ("awaiting_settlements" if selected_count
                                                else "awaiting_selections"),
                "selected_count": selected_count, "settled_count": 0,
                "realized_roi": None,
                "note": ("Selected observations have not settled yet." if selected_count else
                         "No prospective strategy selections have been recorded yet.")}

    def aggregate(items: list[dict]) -> dict:
        outcomes = Counter(str(row.get("result") or "").lower() for row in items)
        return {"count": len(items), "wins": outcomes["win"], "losses": outcomes["loss"],
                "pushes": outcomes["push"],
                "paper_profit_loss": sum(float(row.get("fixed_unit_profit_loss") or 0) for row in items)}

    def groups(key) -> dict:
        names = sorted({str(key(row) or "unknown") for row in settled_rows})
        return {name: aggregate([row for row in settled_rows if str(key(row) or "unknown") == name])
                for name in names}

    result = aggregate(settled_rows)
    total_staked = sum(float(row.get("fixed_unit") or 0) for row in settled_rows)
    result["realized_roi"] = result["paper_profit_loss"] / total_staked if total_staked else None
    result["settled_count"] = result.pop("count")
    result.update({"available": True, "status": "available", "selected_count": selected_count})
    result["by_market"] = groups(lambda row: row["canonical_market"])
    result["by_edge_tier"] = groups(lambda row: row["edge_bucket"])
    result["by_capture_window"] = groups(lambda row: (row.get("context") or {}).get("capture_slot"))
    return result


def _model_evaluation(rows: list[dict]) -> dict | None:
    """Return calibration over raw settled side observations, never P&L.

    OVER and UNDER are intentionally separate observations.  Their binary
    Brier terms are mathematically valid model-calibration terms, although
    paired terms are correlated and must not be described as independent bets.
    Pushes have no binary event outcome and are excluded.
    """
    settled = [row for row in rows if row.get("settled_at_utc") is not None]
    binary = [row for row in settled if str(row.get("result")).lower() in {"win", "loss"}
              and row.get("model_probability") is not None]
    return ({"evaluation_unit": "raw_side_observation", "settled_observation_count": len(settled),
             "brier_observation_count": len(binary),
             "brier_score": (sum((float(row["model_probability"]) -
                                    (1.0 if str(row["result"]).lower() == "win" else 0.0)) ** 2
                                   for row in binary) / len(binary) if binary else None),
             "note": "Model calibration over raw settled side observations; paired sides are not a betting record."}
            if settled else None)


@router.get("/executions")
def list_executions(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                    repo: ResearchRepository = Depends(get_repository)):
    return {"items": repo.executions(limit, offset), "limit": limit, "offset": offset}


@router.get("/observations")
def list_observations(filters: dict = Depends(filter_params), limit: int = Query(50, ge=1, le=200),
                      offset: int = Query(0, ge=0), repo: ResearchRepository = Depends(get_repository)):
    return {"items": repo.observations(filters, limit, offset), "limit": limit, "offset": offset}


@router.get("/observations/{observation_id}")
def get_observation(observation_id: str, repo: ResearchRepository = Depends(get_repository)):
    row = repo.observation(observation_id)
    if row is None: raise HTTPException(404, "Observation not found")
    return row


@router.get("/observations/{observation_id}/timeline")
def observation_timeline(observation_id: str, repo: ResearchRepository = Depends(get_repository)):
    rows = repo.timeline(observation_id)
    if rows is None:
        raise HTTPException(404, "Observation not found")
    return {"items": rows}


@router.get("/observations/{observation_id}/context")
def observation_context(observation_id: str, as_of: datetime | None = None,
                        repo: ResearchRepository = Depends(get_repository)):
    """Context known at ``as_of`` (default now); never refreshes or mutates data."""
    result = repo.context(observation_id, as_of)
    if result is None:
        raise HTTPException(404, "Observation not found")
    return result


@router.get("/summary")
def summary(repo: ResearchRepository = Depends(get_repository)):
    today = datetime.now(timezone.utc).date()
    with repo.engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(
            observations, *[c for c in settlements.c if c.name not in {"observation_id", "line", "side"}]
        ).outerjoin(settlements, observations.c.observation_id == settlements.c.observation_id))]
        slots = list(conn.execute(select(scheduler_slots.c.status)))
        canonical_decision_ids = conn.execute(select(opportunity_decisions.c.decision_id)).all()
        canonical_ids = conn.execute(select(opportunity_decisions.c.decision_id).where(
            opportunity_decisions.c.action != "PASS")).all()
        canonical_join = (opportunity_decisions.join(observations,
            opportunity_decisions.c.selected_observation_id == observations.c.observation_id).join(
            settlements, opportunity_decisions.c.selected_observation_id == settlements.c.observation_id))
        canonical_rows = [dict(r._mapping) for r in conn.execute(select(
            observations, *[c for c in settlements.c if c.name not in {"observation_id", "line", "side"}]
        ).select_from(canonical_join).where(opportunity_decisions.c.action != "PASS"))]
        journals = [dict(row._mapping) for row in conn.execute(select(selection_journal).order_by(
            selection_journal.c.selected_at_utc, selection_journal.c.journal_id))]
        latest_journal = {row["observation_id"]: row for row in journals}
        legacy_selected_ids = sorted(observation_id for observation_id, row in latest_journal.items()
                                     if row["selected"])
        if canonical_decision_ids:
            selected_count, selected_rows = len(canonical_ids), canonical_rows
        else:
            selected_count = len(legacy_selected_ids)
            legacy_join = observations.join(settlements,
                observations.c.observation_id == settlements.c.observation_id)
            selected_rows = ([dict(r._mapping) for r in conn.execute(select(
                observations, *[c for c in settlements.c if c.name not in {"observation_id", "line", "side"}]
            ).select_from(legacy_join).where(observations.c.observation_id.in_(legacy_selected_ids)))]
                if legacy_selected_ids else [])
        pass_rows = [dict(row._mapping) for row in conn.execute(select(
            opportunity_decisions.c.reason_codes).where(opportunity_decisions.c.action == "PASS"))]
        executions = repo.executions(200, 0)
    settled_count = sum(r["settled_at_utc"] is not None for r in rows)
    successful = next((e for e in executions if e["completed_count"] > 0), None)
    latest = executions[0] if executions else None
    return {"total_observations": len(rows), "observations_today": sum(_utc(r["observed_at_utc"]).date() == today for r in rows),
        "observations_by_market": dict(Counter(r["canonical_market"] for r in rows)),
        "observations_by_capture_window": dict(Counter((r.get("context") or {}).get("capture_slot", "unknown") for r in rows)),
        "observations_by_edge_tier": dict(Counter(r["edge_bucket"] for r in rows)),
        "settlement_counts": {"settled": settled_count, "unsettled": len(rows)-settled_count},
        "scheduler_capture_counts": {k: sum(s.status == k for s in slots) for k in ("failed", "deferred")},
        "latest_successful_execution": successful, "latest_feature_build_timestamp": max((_utc(r["feature_built_at_utc"]) for r in rows), default=None),
        "latest_observation_timestamp": max((_utc(r["observed_at_utc"]) for r in rows), default=None),
        "represented_matchups": len({r["game_id"] for r in rows}),
        "current_model_versions_observed": sorted({r["model_version"] for r in rows}),
        "latest_scheduler_execution_timestamp": latest["started_at_utc"] if latest else None,
        "latest_quota_state": ({"before": latest["quota_before"], "after": latest["quota_after"]} if latest else None),
        "model_evaluation": _model_evaluation(rows),
        "strategy_performance": _strategy_performance(selected_rows, selected_count),
        "selection_decisions": {"selected_count": selected_count, "pass_count": len(pass_rows),
            "pass_reason_counts": dict(Counter(code for row in pass_rows
                                                for code in (row.get("reason_codes") or [])))}}

@router.get("/historical-performance")
def historical_performance(repo: ResearchRepository = Depends(get_repository)):
    return repo.historical_performance()


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def dashboard(repo: ResearchRepository = Depends(get_repository)):
    # Keep presentation static: all live values come from the existing read-only JSON API.
    return HTMLResponse(DASHBOARD_HTML)
