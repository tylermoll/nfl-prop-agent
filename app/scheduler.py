"""Quota-aware, read-only prospective shadow capture orchestration.

The module contains no account, wagering, or order APIs.  Expensive inputs are
dependency-injected so tests and dry-runs cannot accidentally contact markets.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.providers.the_odds_api import _parse_datetime


@dataclass(frozen=True)
class CaptureWindow:
    name: str
    before_kickoff: timedelta
    tolerance: timedelta = timedelta(minutes=10)


DEFAULT_WINDOWS = (
    CaptureWindow("24h", timedelta(hours=24)), CaptureWindow("6h", timedelta(hours=6)),
    CaptureWindow("90m", timedelta(minutes=90)), CaptureWindow("15m", timedelta(minutes=15)),
)
PRIORITY = {"15m": 0, "90m": 1, "6h": 2, "24h": 3, "final": -1}


@dataclass(frozen=True)
class SchedulerConfig:
    windows: tuple[CaptureWindow, ...] = DEFAULT_WINDOWS
    final_window: CaptureWindow | None = None
    minimum_quota_reserve: int = 25
    max_credits_per_execution: int = 12
    max_games_per_execution: int = 4
    retry_budget: int = 2
    credits_per_event: int = 3
    provider_stale_after: timedelta = timedelta(minutes=5)
    model_stale_after: timedelta = timedelta(hours=1)


@dataclass(frozen=True)
class DueCapture:
    event_id: str
    kickoff_utc: datetime
    slot: str
    target_time_utc: datetime


def due_captures(events: list[dict[str, Any]], now: datetime, config: SchedulerConfig,
                 state: Callable[[str, str, datetime], dict | None]) -> list[DueCapture]:
    """Select unstarted slots within ± tolerance, suppressing successes/exhausted retries."""
    now = now.astimezone(timezone.utc)
    windows = config.windows + ((config.final_window,) if config.final_window else ())
    output: list[DueCapture] = []
    for event in events:
        event_id, kickoff = event.get("id"), _parse_datetime(event.get("commence_time"))
        if not isinstance(event_id, str) or not event_id or kickoff is None or now >= kickoff:
            continue
        for window in windows:
            target = kickoff - window.before_kickoff
            if abs((now - target).total_seconds()) > window.tolerance.total_seconds():
                continue
            prior = state(event_id, window.name, target)
            if prior and (prior["status"] == "completed" or int(prior.get("attempts", 0)) >= config.retry_budget):
                continue
            output.append(DueCapture(event_id, kickoff, window.name, target))
    return sorted(output, key=lambda x: (PRIORITY.get(x.slot, 99), x.kickoff_utc, x.event_id))


def sanitized_error(exc: BaseException) -> str:
    text = re.sub(r"(?i)(api[_-]?key|token|authorization|password|credential)\s*[=:]?\s*(?:bearer\s+)?[^&\s,;]*",
                  r"\1=[REDACTED]", str(exc))
    text = re.sub(r"https?://\S+", "[URL REDACTED]", text)
    return f"{type(exc).__name__}: {text}"[:500]


def failure_category(exc: BaseException, *, stage: str) -> str:
    """Map existing capture failures to stable, deliberately coarse diagnostics."""
    message = str(exc).lower()
    name = type(exc).__name__.lower()
    if stage == "provider":
        if "json" in message or "payload" in message or "response lacks" in message or "must be a json" in message:
            return "provider_payload_invalid"
        return "provider_http_failure"
    if stage == "database":
        return "database_write_failure"
    if "missing hard rock" in message:
        return "hardrock_markets_unavailable"
    if "player" in message and ("identity" in message or "ambiguous" in message):
        return "player_identity_mismatch"
    if "event identity" in message or "matchup" in message or "feature schema" in message:
        return "feature_identity_mismatch"
    if "no eligible" in message or "no matching" in message:
        return "no_matching_feature_rows"
    if stage == "model" or "model" in message or "scor" in name or "artifact" in message:
        return "model_scoring_failure"
    return "unknown_capture_failure"


def freshness_metadata(*, captured_at: datetime, provider_observed_at: datetime | None,
                       provider_updated_at: datetime | None, feature_built_at: datetime | None,
                       config: SchedulerConfig) -> dict[str, Any]:
    """Produce explicit ages/stale flags without substituting absent timestamps."""
    def age(value: datetime | None) -> float | None:
        return None if value is None else max(0.0, (captured_at - value.astimezone(timezone.utc)).total_seconds())
    observed_age, updated_age, model_age = age(provider_observed_at), age(provider_updated_at), age(feature_built_at)
    provider_age = updated_age if updated_age is not None else observed_age
    return {"provider_observed_at_utc": provider_observed_at.isoformat() if provider_observed_at else None,
        "provider_updated_at_utc": provider_updated_at.isoformat() if provider_updated_at else None,
        "provider_age_seconds": provider_age,
        "provider_stale": provider_age is None or provider_age > config.provider_stale_after.total_seconds(),
        "model_feature_timestamp_utc": feature_built_at.isoformat() if feature_built_at else None,
        "model_feature_age_seconds": model_age,
        "model_stale": model_age is None or model_age > config.model_stale_after.total_seconds()}


class SnapshotScheduler:
    """Coordinates one discovery, targeted odds calls, and shared context loads.

    ``build_observations`` receives a capture, its event odds, the shared Kalshi
    rows, and the shared model/artifact object. It returns append-ready shadow
    rows. Hard Rock rows may be persisted with empty reference/Kalshi context;
    missing values are flagged, never manufactured.
    """
    def __init__(self, *, odds: Any, store: Any, kalshi: Any,
                 load_model: Callable[[], Any],
                 build_observations: Callable[[DueCapture, list, list, Any, datetime], list[dict]],
                 config: SchedulerConfig = SchedulerConfig(), now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.odds, self.store, self.kalshi = odds, store, kalshi
        self.load_model, self.build_observations = load_model, build_observations
        self.config, self.now = config, now

    async def run(self, *, dry_run: bool = False) -> dict[str, Any]:
        started, execution_id = self.now().astimezone(timezone.utc), str(uuid4())
        report: dict[str, Any] = {"execution_id": execution_id, "started_at_utc": started.isoformat(),
            "dry_run": dry_run, "due": [], "selected": [], "completed": [], "deferred": [], "failed": [],
            "estimated_credits": 0, "actual_credits_consumed": 0, "quota_before": None, "quota_after": None,
            "http_request_count": 0, "errors": []}
        try:
            events = await self.odds.discover_events()
            event_context = {event.get("id"): event for event in events if isinstance(event, dict)}
            due = due_captures(events, started, self.config, self.store.slot_state)
            report["due"] = [asdict_json(x) for x in due]
            remaining = self.odds.usage.get("requests_remaining")
            report["quota_before"] = remaining
            selected: list[DueCapture] = []
            games: set[str] = set()
            credits = 0
            for capture in due:
                reason = None
                incremental = 0 if capture.event_id in games else self.config.credits_per_event
                if capture.event_id not in games and len(games) >= self.config.max_games_per_execution:
                    reason = "maximum_games_budget"
                elif credits + incremental > self.config.max_credits_per_execution:
                    reason = "per_run_credit_budget"
                elif remaining is not None and remaining - credits - incremental < self.config.minimum_quota_reserve:
                    reason = "quota_reserve"
                if reason:
                    item = {**asdict_json(capture), "reason": reason}; report["deferred"].append(item)
                    if not dry_run: self._record(capture, "deferred", reason, started, increment=False)
                else:
                    selected.append(capture); games.add(capture.event_id); credits += incremental
            report["selected"], report["estimated_credits"] = [asdict_json(x) for x in selected], credits
            if dry_run or not selected:
                return self._finish(report, started, persist=not dry_run)
            try:
                kalshi_rows = await self.kalshi.fetch_nfl_player_props()
            except Exception as exc:
                kalshi_rows = []; report["errors"].append({"kind": "kalshi_failure", "message": sanitized_error(exc)})
            try:
                model = self.load_model()
            except Exception as exc:
                model = None; report["errors"].append({"kind": "football_model_scoring_failure", "message": sanitized_error(exc)})
            odds_by_event: dict[str, list] = {}
            provider_failures: dict[str, BaseException] = {}
            event_costs: dict[str, dict[str, Any]] = {}
            ordered_events = tuple(dict.fromkeys(c.event_id for c in selected))
            for event_id in ordered_events:
                before_requests = int(self.odds.usage.get("http_requests") or 0)
                before_credits = int(self.odds.usage.get("quota_consumed") or 0)
                try: odds_by_event[event_id] = await self.odds.fetch_event_player_props(event_id)
                except Exception as exc:
                    odds_by_event[event_id] = []
                    provider_failures[event_id] = exc
                    report["errors"].append({"kind": "odds_api_failure", "message": sanitized_error(exc)})
                event_costs[event_id] = {
                    "provider_request_made": int(self.odds.usage.get("http_requests") or 0) > before_requests,
                    "actual_credit_cost": max(0, int(self.odds.usage.get("quota_consumed") or 0) - before_credits),
                }
            actual = int(self.odds.usage.get("quota_consumed") or 0)
            report["actual_credits_consumed"] = actual
            charged_events: set[str] = set()
            for capture in selected:
                stage = "capture"
                try:
                    if capture.event_id in provider_failures:
                        stage = "provider"
                        raise provider_failures[capture.event_id]
                    rows = odds_by_event[capture.event_id]
                    hardrock = [r for r in rows if r.source == self.odds.bookmakers[0]]
                    if not hardrock: raise RuntimeError("missing Hard Rock market")
                    if model is None:
                        stage = "model"
                        raise RuntimeError("football model unavailable")
                    stage = "build"
                    observations = self.build_observations(capture, rows, kalshi_rows, model, self.now())
                    if not observations: raise RuntimeError("no eligible Hard Rock observations")
                    stage = "database"
                    if hasattr(self.store, "complete_capture"):
                        old = self.store.slot_state(capture.event_id, capture.slot, capture.target_time_utc)
                        self.store.complete_capture(observations, {"event_id": capture.event_id, "slot": capture.slot,
                            "target_time_utc": capture.target_time_utc, "status": "completed",
                            "attempts": int(old.get("attempts", 0) if old else 0) + 1, "reason": None,
                            "updated_at_utc": self.now()})
                    else:
                        for row in observations: self.store.append(row)
                        self._record(capture, "completed", None, self.now(), increment=True)
                    report["completed"].append(asdict_json(capture))
                except Exception as exc:
                    message = sanitized_error(exc)
                    old = self.store.slot_state(capture.event_id, capture.slot, capture.target_time_utc)
                    attempts = int(old.get("attempts", 0) if old else 0) + 1
                    context = event_context.get(capture.event_id) or {}
                    costs = event_costs.get(capture.event_id, {})
                    attributable = capture.event_id not in charged_events
                    detail = {**asdict_json(capture),
                        "provider_event_id": capture.event_id,
                        "matchup": event_matchup(context),
                        "failure_category": failure_category(exc, stage=stage),
                        "reason": message,
                        "provider_request_made": bool(costs.get("provider_request_made")),
                        "estimated_credit_cost": self.config.credits_per_event if attributable else 0,
                        "actual_credit_cost": int(costs.get("actual_credit_cost", 0)) if attributable else 0,
                        "retryable": attempts < self.config.retry_budget and
                            self.now().astimezone(timezone.utc) <= capture.target_time_utc + next(
                                w.tolerance for w in self.config.windows + ((self.config.final_window,) if self.config.final_window else ())
                                if w.name == capture.slot) and self.now().astimezone(timezone.utc) < capture.kickoff_utc}
                    charged_events.add(capture.event_id)
                    report["failed"].append(detail)
                    self._record(capture, "failed", message, self.now(), increment=True)
            return self._finish(report, started, persist=True)
        except Exception as exc:
            report["errors"].append({"kind": "scheduler_failure", "message": sanitized_error(exc)})
            return self._finish(report, started, persist=not dry_run)

    def _record(self, capture: DueCapture, status: str, reason: str | None, now: datetime, increment: bool) -> None:
        old = self.store.slot_state(capture.event_id, capture.slot, capture.target_time_utc)
        self.store.record_slot({"event_id": capture.event_id, "slot": capture.slot,
            "target_time_utc": capture.target_time_utc, "status": status,
            "attempts": int(old.get("attempts", 0) if old else 0) + int(increment), "reason": reason,
            "updated_at_utc": now})

    def _finish(self, report: dict, started: datetime, *, persist: bool) -> dict:
        report["ended_at_utc"] = self.now().astimezone(timezone.utc).isoformat()
        report["quota_after"] = self.odds.usage.get("requests_remaining")
        report["http_request_count"] = self.odds.usage.get("http_requests", 0)
        if persist: self.store.record_execution({"execution_id": report["execution_id"], "started_at_utc": started,
            "ended_at_utc": datetime.fromisoformat(report["ended_at_utc"]), "details": report})
        return report


def asdict_json(capture: DueCapture) -> dict[str, Any]:
    return {"event_id": capture.event_id, "kickoff_utc": capture.kickoff_utc.isoformat(),
            "slot": capture.slot, "target_time_utc": capture.target_time_utc.isoformat()}


def event_matchup(event: dict[str, Any]) -> str | None:
    away, home = event.get("away_team"), event.get("home_team")
    return f"{away} at {home}" if isinstance(away, str) and isinstance(home, str) else None
