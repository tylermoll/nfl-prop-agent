"""Read-only, sanitized views over persisted shadow-research data."""
from __future__ import annotations

import html
import re
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, create_engine, or_, select

from app.config import settings
from app.shadow_storage import normalize_database_url, observations, scheduler_executions, scheduler_slots, settlements

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
        query = select(observations, *[c for c in settlements.c if c.name != "observation_id"]).outerjoin(
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
            rows = conn.execute(query.limit(limit).offset(offset))
            return [observation_view(dict(row._mapping), detail=False) for row in rows]

    def observation(self, observation_id: str) -> dict | None:
        query = self.observation_query({}).where(observations.c.observation_id == observation_id)
        with self.engine.connect() as conn:
            row = conn.execute(query).first()
            return observation_view(dict(row._mapping), detail=True) if row else None


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
            "http_request_count": detail.get("http_request_count", 0), "errors": detail.get("errors", [])}


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
                        "paper_fixed_unit": row.get("fixed_unit"), "paper_profit_loss": row.get("fixed_unit_profit_loss")}
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


@router.get("/summary")
def summary(repo: ResearchRepository = Depends(get_repository)):
    today = datetime.now(timezone.utc).date()
    with repo.engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(observations, settlements.c.settled_at_utc).outerjoin(
            settlements, observations.c.observation_id == settlements.c.observation_id))]
        slots = list(conn.execute(select(scheduler_slots.c.status)))
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
        "current_model_versions_observed": sorted({r["model_version"] for r in rows}),
        "latest_quota_state": ({"before": latest["quota_before"], "after": latest["quota_after"]} if latest else None),
        "performance_metrics": None, "performance_note": "Performance is not reported unless observations are settled."}


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def dashboard(repo: ResearchRepository = Depends(get_repository)):
    data = summary(repo)
    recent = repo.observations({}, 10, 0)
    rows = "".join(f"<tr><td>{html.escape(str(x['captured_at_utc']))}</td><td>{html.escape(x['player']['name'])}</td>"
                   f"<td>{html.escape(x['canonical_market'])}</td><td>{html.escape(x['edge_tier'])}</td></tr>" for x in recent)
    return HTMLResponse(f"<!doctype html><html><head><title>NFL research observations</title><style>body{{font:16px system-ui;max-width:1100px;margin:2rem auto}}table{{border-collapse:collapse;width:100%}}td,th{{padding:.5rem;border-bottom:1px solid #ddd;text-align:left}}</style></head><body><h1>Production research observations</h1><p>Descriptive, read-only research views — not betting recommendations.</p><h2>Health</h2><p>Total observations: <b>{data['total_observations']}</b> · Settled: <b>{data['settlement_counts']['settled']}</b> · Latest observation: <b>{data['latest_observation_timestamp'] or 'none'}</b></p><h2>Latest captures</h2><table><thead><tr><th>Captured (UTC)</th><th>Player</th><th>Market</th><th>Edge tier</th></tr></thead><tbody>{rows}</tbody></table><p><a href='/research/summary'>JSON summary</a> · <a href='/docs'>API documentation</a></p></body></html>")
