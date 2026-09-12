"""Production, read-only adapter for :mod:`app.scheduler`.

The scheduler deliberately owns provider I/O.  This module only loads local,
versioned model/data inputs and converts the supplied quotes into immutable
shadow observations; it has no network or transaction API.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

import pandas as pd

from app.config import settings
from app.identities import canonical_event_identity, canonical_team, normalize_player_name
from app.math_utils import no_vig_probabilities
from app.modeling.benchmark import MARKETS, validate_feature_names
from app.modeling.live import FootballArtifactScorer
from app.models import Side
from app.scheduler import DueCapture, SchedulerConfig, freshness_metadata
from app.shadow import make_observation

logger = logging.getLogger(__name__)


class IdentityResolutionError(ValueError):
    """A safe, deterministic rejection of one provider prop identity."""

    non_retryable = True

    def __init__(self, stage: str, message: str, diagnostics: dict[str, Any]):
        super().__init__(message)
        self.identity_stage = stage
        self.diagnostics = {**diagnostics, "identity_stage": stage}


class ObservationBuildResult(list):
    """Observations plus safe per-prop rejections for scheduler reporting."""

    def __init__(self, rows: list[dict], rejected_props: list[dict[str, Any]]):
        super().__init__(rows)
        self.rejected_props = rejected_props


def _safe_text(value: Any, limit: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    # Provider identifiers and names are useful operational metadata, but must
    # never be allowed to inject multiline payloads or credential-like values.
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    value = re.sub(r"(?i)(api[_-]?key|token|authorization|password|credential)\s*[=:]\s*\S+",
                   r"\1=[REDACTED]", value)
    return value[:limit] or None


@dataclass(frozen=True)
class ProductionModels:
    scorers: dict[str, FootballArtifactScorer]
    provenance: dict[str, dict[str, Any]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _artifact_paths() -> dict[str, str | None]:
    return {
        "player_pass_yds": settings.football_artifact_player_pass_yds,
        "player_reception_yds": settings.football_artifact_player_reception_yds,
        "player_receptions": settings.football_artifact_player_receptions,
    }


def load_production_models() -> ProductionModels:
    """Load all three local artifacts atomically, failing closed on any error."""
    scorers: dict[str, FootballArtifactScorer] = {}
    provenance: dict[str, dict[str, Any]] = {}
    for market, configured in _artifact_paths().items():
        if not configured:
            raise RuntimeError(f"artifact path is not configured for {market}")
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"required artifact is absent for {market}: {path}")
        scorer = FootballArtifactScorer(path)
        features = validate_feature_names(scorer.artifact["features"])
        if not features or len(features) != len(set(features)):
            raise ValueError(f"invalid feature schema in artifact for {market}")
        scorers[market] = scorer
        provenance[market] = {"market": market, "artifact_path": str(path),
                              "artifact_sha256": _sha256(path), "model_version": scorer.version,
                              "required_features": features}
    if set(scorers) != set(MARKETS):
        raise RuntimeError("the complete football-only artifact set is required")
    return ProductionModels(scorers, provenance)


class CurrentFeatureCache:
    """Strict reader for an externally refreshed leakage-safe current feature table.

    Rows must carry a stable nflverse player ID, matchup identity, kickoff, and
    both build/data-cutoff timestamps.  No current-game result column is allowed.
    """
    IDENTITY_COLUMNS = {"player_id", "player_name", "canonical_market", "kickoff",
                        "home_team", "away_team", "feature_built_at_utc", "feature_data_as_of_utc"}
    RESULT_COLUMNS = {"actual_value", "passing_yards", "receiving_yards", "receptions",
                      "attempts", "dropbacks", "targets", "team_pass_attempts"}

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"current feature cache is absent: {self.path}")
        suffix = self.path.suffix.lower()
        if suffix in {".parquet", ".pq"}:
            self.rows = pd.read_parquet(self.path)
        elif suffix == ".json":
            self.rows = pd.read_json(self.path)
        elif suffix == ".csv":
            self.rows = pd.read_csv(self.path)
        else:
            raise ValueError("current feature cache must be parquet, JSON, or CSV")
        missing = self.IDENTITY_COLUMNS - set(self.rows)
        forbidden = self.RESULT_COLUMNS & set(self.rows)
        if missing or forbidden:
            raise ValueError(f"unsafe current feature cache (missing={sorted(missing)}, result_columns={sorted(forbidden)})")
        if self.rows.player_id.isna().any() or self.rows.player_id.astype(str).str.strip().eq("").any():
            raise ValueError("stable nflverse player_id is required for every current feature row")
        self.rows["_kickoff"] = pd.to_datetime(self.rows.kickoff, utc=True, errors="raise")
        self.rows["_built"] = pd.to_datetime(self.rows.feature_built_at_utc, utc=True, errors="raise")
        self.rows["_asof"] = pd.to_datetime(self.rows.feature_data_as_of_utc, utc=True, errors="raise")
        self.rows["_player"] = self.rows.player_name.map(normalize_player_name)
        self.rows["_event"] = self.rows.apply(
            lambda r: canonical_event_identity(r.away_team, r.home_team), axis=1)
        if self.rows._event.isna().any():
            raise ValueError("current feature cache contains an unresolved NFL matchup")
        if ((self.rows._asof >= self.rows._kickoff) | (self.rows._built >= self.rows._kickoff)).any():
            raise ValueError("feature cache contains information timestamped at/after kickoff")

    def row(self, quote: Any, capture: DueCapture, required_features: list[str], now: datetime):
        market = quote.market_type.value
        event = canonical_event_identity(quote.away_team, quote.home_team)
        base = {"provider_event_id": _safe_text(getattr(quote, "game_id", None)),
                "canonical_event": event,
                "provider_player_name": _safe_text(getattr(quote, "player_name", None)),
                "provider_player_identifier": _safe_text(getattr(quote, "source_market_id", None)),
                "candidate_stable_ids": [], "candidate_feature_row_count": 0,
                "canonical_market": market}
        if getattr(quote, "game_id", None) != capture.event_id:
            raise IdentityResolutionError("provider_event", "provider event does not match scheduled event", base)
        if event is None:
            raise IdentityResolutionError("canonical_event", "provider event identity cannot be established", base)

        # Resolve the provider name to a stable ID first, within the exact game.
        # Do not let market availability influence player identity and never
        # search another kickoff or matchup as a fallback.
        game_rows = self.rows.loc[(self.rows._event == event) &
                                  (self.rows._kickoff == capture.kickoff_utc)]
        if game_rows.empty:
            raise IdentityResolutionError("current_game", "no current-feature rows for the exact game", base)
        name_rows = game_rows.loc[game_rows._player == normalize_player_name(quote.player_name)]
        stable_ids = sorted(set(name_rows.player_id.astype(str)))
        base["candidate_stable_ids"] = stable_ids
        base["candidate_feature_row_count"] = int(
            (name_rows.canonical_market == market).sum())
        if len(stable_ids) != 1:
            raise IdentityResolutionError("stable_player", "stable player identity is missing or ambiguous", base)
        selected = game_rows.loc[(game_rows.player_id.astype(str) == stable_ids[0]) &
                                 (game_rows.canonical_market == market)]
        base["candidate_feature_row_count"] = int(len(selected))
        if len(selected) != 1:
            raise IdentityResolutionError("current_feature_row",
                "current-game feature row is missing or ambiguous", base)
        item = selected.iloc[0]
        missing = [feature for feature in required_features if feature not in self.rows]
        if missing:
            raise ValueError(f"live feature schema does not match artifact: {missing}")
        now = now.astimezone(timezone.utc)
        if item._asof.to_pydatetime() > now or item._built.to_pydatetime() > now:
            raise ValueError("feature row was built with information unavailable at capture time")
        return pd.DataFrame([{f: item[f] for f in required_features}]), item


def _reference_context(rows: list[Any], hardrock: Any, target: str) -> dict[str, Any]:
    books: dict[str, list[Any]] = {}
    for row in rows:
        if (row.source != target and row.game_id == hardrock.game_id and
                normalize_player_name(row.player_name) == normalize_player_name(hardrock.player_name) and
                row.market_type == hardrock.market_type and row.line == hardrock.line):
            books.setdefault(row.source, []).append(row)
    quotes, over = {}, []
    for book, items in books.items():
        prices = {r.side.value: r.american_odds for r in items if r.side in (Side.OVER, Side.UNDER)}
        if prices.get("over") is None or prices.get("under") is None:
            continue
        probability = no_vig_probabilities(prices["over"], prices["under"])[0]
        quotes[book] = {"over_odds": prices["over"], "under_odds": prices["under"],
                        "exact_threshold_over_no_vig_probability": probability}
        over.append(probability)
    return {"books": quotes, "reference_book_count": len(quotes),
            "exact_threshold_over_no_vig_probability": float(median(over)) if over else None,
            "insufficient_reference_coverage": len(quotes) < settings.consensus_min_reference_books}


def _kalshi_context(rows: list[Any], hardrock: Any) -> dict[str, Any]:
    event = canonical_event_identity(hardrock.away_team, hardrock.home_team)
    matches = [r for r in rows if canonical_event_identity(r.away_team, r.home_team) == event and
               normalize_player_name(r.player_name) == normalize_player_name(hardrock.player_name) and
               r.market_type == hardrock.market_type and r.line == hardrock.line]
    if len(matches) != 1:
        return {"kalshi_unavailable": True}
    row = matches[0]
    return {"kalshi_unavailable": False, "source_market_id": row.source_market_id,
            "midpoint": row.contract_price, "yes_bid": row.yes_bid, "yes_ask": row.yes_ask,
            "no_bid": row.no_bid, "no_ask": row.no_ask, "volume": row.volume,
            "open_interest": row.open_interest}


def create_scheduler_pipeline():
    """Return the exact ``(model_loader, observation_builder)`` scheduler contract."""
    loaded: ProductionModels | None = None
    feature_cache: CurrentFeatureCache | None = None

    def model_loader() -> ProductionModels:
        nonlocal loaded
        if loaded is None:
            loaded = load_production_models()
        return loaded

    def observation_builder(capture: DueCapture, rows: list[Any], kalshi_rows: list[Any],
                            models: ProductionModels, now: datetime) -> list[dict]:
        nonlocal feature_cache
        if feature_cache is None:
            if not settings.football_current_feature_path:
                raise RuntimeError("FOOTBALL_CURRENT_FEATURE_PATH is not configured")
            feature_cache = CurrentFeatureCache(settings.football_current_feature_path)
        hardrock = [r for r in rows if r.source == settings.the_odds_api_target_bookmaker and
                    r.line is not None and r.american_odds is not None and r.side in (Side.OVER, Side.UNDER)]
        output, rejected = [], []
        for quote in hardrock:
            market = quote.market_type.value
            scorer = models.scorers[market]
            try:
                features, identity = feature_cache.row(quote, capture, scorer.artifact["features"], now)
            except IdentityResolutionError as exc:
                rejected.append(exc.diagnostics)
                logger.warning("Rejecting unresolved Hard Rock prop: %s", exc.diagnostics)
                continue
            built = identity._built.to_pydatetime()
            score = scorer.score(features, quote.line, built)
            reference = _reference_context(rows, quote, settings.the_odds_api_target_bookmaker)
            kalshi = _kalshi_context(kalshi_rows, quote)
            fresh = freshness_metadata(captured_at=now, provider_observed_at=quote.observed_at_utc,
                provider_updated_at=quote.source_updated_at_utc, feature_built_at=built,
                config=SchedulerConfig(provider_stale_after=timedelta(seconds=settings.scheduler_provider_stale_seconds),
                                       model_stale_after=timedelta(seconds=settings.scheduler_model_stale_seconds)))
            output.append(make_observation(observed_at_utc=now, kickoff_utc=capture.kickoff_utc,
                game_id=capture.event_id, player_id=str(identity.player_id), player_name=quote.player_name,
                team=canonical_team(identity.get("team")) if "team" in identity else None,
                opponent=canonical_team(identity.get("opponent")) if "opponent" in identity else None,
                canonical_market=market, line=quote.line, side=quote.side.value,
                offered_odds=quote.american_odds, model_score=score, reference_context=reference,
                kalshi_context=kalshi, source_ids={"hardrock": quote.source_market_id}, freshness=fresh,
                context={"capture_slot": capture.slot, "capture_target_time_utc": capture.target_time_utc.isoformat(),
                         "feature_data_as_of_utc": identity._asof.isoformat(),
                         "artifact_provenance": models.provenance[market]}))
        if not output and rejected:
            raise IdentityResolutionError("event_props", "all eligible Hard Rock props failed deterministic identity resolution",
                                          {"rejected_props": rejected})
        return ObservationBuildResult(output, rejected)

    return model_loader, observation_builder
