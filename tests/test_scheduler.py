import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.scheduler import (DEFAULT_WINDOWS, CaptureWindow, SchedulerConfig, SnapshotScheduler,
                           due_captures, freshness_metadata, sanitized_error)

NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)

class Store:
    def __init__(self): self.states, self.observations, self.executions = {}, [], []
    def slot_state(self, event, slot, target): return self.states.get((event, slot, target))
    def record_slot(self, row): self.states[(row["event_id"], row["slot"], row["target_time_utc"])] = row
    def append(self, row): self.observations.append(row)
    def record_execution(self, row): self.executions.append(row)

class Odds:
    bookmakers = ("hardrockbet", "fanduel", "draftkings")
    def __init__(self, events):
        self.events, self.targeted = events, []
        self.usage = {"requests_remaining": 100, "http_requests": 0, "quota_consumed": 0}
    async def discover_events(self): self.usage["http_requests"] += 1; return self.events
    async def fetch_event_player_props(self, event):
        self.targeted.append(event); self.usage["http_requests"] += 1; self.usage["quota_consumed"] += 3
        return [SimpleNamespace(source="hardrockbet", observed_at_utc=NOW, source_updated_at_utc=NOW)]

class Kalshi:
    def __init__(self): self.calls = 0
    async def fetch_nfl_player_props(self): self.calls += 1; return []

def event(slot, ident="g"):
    window = next(w for w in DEFAULT_WINDOWS if w.name == slot)
    return {"id": ident, "commence_time": (NOW + window.before_kickoff).isoformat(),
            "away_team": "Buffalo Bills", "home_team": "Miami Dolphins"}

@pytest.mark.parametrize("slot", ["24h", "6h", "90m", "15m"])
def test_slot_determination(slot): assert due_captures([event(slot)], NOW, SchedulerConfig(), lambda *_: None)[0].slot == slot

def test_tolerance_and_post_kickoff():
    cfg = SchedulerConfig(windows=(CaptureWindow("x", timedelta(hours=1), timedelta(minutes=5)),))
    assert due_captures([{"id":"g", "commence_time":(NOW+timedelta(minutes=64)).isoformat()}], NOW, cfg, lambda *_:None)
    assert not due_captures([{"id":"g", "commence_time":(NOW+timedelta(minutes=66)).isoformat()}], NOW, cfg, lambda *_:None)
    assert not due_captures([{"id":"g", "commence_time":NOW.isoformat()}], NOW, cfg, lambda *_:None)

def test_completed_suppressed_and_failed_retryable():
    capture = due_captures([event("15m")], NOW, SchedulerConfig(), lambda *_:None)[0]
    completed = lambda *_: {"status":"completed", "attempts":1}
    retry = lambda *_: {"status":"failed", "attempts":1}
    assert not due_captures([event("15m")], NOW, SchedulerConfig(), completed)
    assert due_captures([event("15m")], NOW, SchedulerConfig(retry_budget=2), retry) == [capture]

def test_priority():
    events = [event("24h","a"), event("15m","b"), event("90m","c")]
    assert [x.slot for x in due_captures(events, NOW, SchedulerConfig(), lambda *_:None)] == ["15m","90m","24h"]

def run_scheduler(events, **config):
    odds, store, kalshi, loads = Odds(events), Store(), Kalshi(), []
    scheduler = SnapshotScheduler(odds=odds, store=store, kalshi=kalshi,
        load_model=lambda: loads.append(1) or object(), build_observations=lambda *args:[{"row":1}],
        config=SchedulerConfig(**config), now=lambda: NOW)
    return asyncio.run(scheduler.run()), odds, store, kalshi, loads

def test_budgets_targeting_and_shared_loads():
    report, odds, _, kalshi, loads = run_scheduler([event("15m","a"),event("90m","b"),event("6h","c")],
        max_games_per_execution=2, max_credits_per_execution=6)
    assert odds.targeted == ["a","b"] and report["deferred"][0]["reason"] == "maximum_games_budget"
    assert kalshi.calls == 1 and len(loads) == 1

def test_quota_reserve_and_credit_budget():
    report, odds, *_ = run_scheduler([event("15m")], minimum_quota_reserve=99)
    assert report["deferred"][0]["reason"] == "quota_reserve" and not odds.targeted
    report, odds, *_ = run_scheduler([event("15m")], max_credits_per_execution=2)
    assert report["deferred"][0]["reason"] == "per_run_credit_budget" and not odds.targeted

def test_dry_run_has_no_expensive_calls_or_persistence():
    odds, store, kalshi = Odds([event("15m")]), Store(), Kalshi()
    scheduler = SnapshotScheduler(odds=odds, store=store, kalshi=kalshi, load_model=lambda:pytest.fail(),
        build_observations=lambda *_:pytest.fail(), now=lambda:NOW)
    report = asyncio.run(scheduler.run(dry_run=True))
    assert report["selected"] and not odds.targeted and kalshi.calls == 0
    assert not store.observations and not store.executions and not store.states

def test_stale_flags_and_sanitization():
    result = freshness_metadata(captured_at=NOW, provider_observed_at=NOW-timedelta(minutes=6),
        provider_updated_at=None, feature_built_at=NOW-timedelta(hours=2), config=SchedulerConfig())
    assert result["provider_stale"] and result["model_stale"]
    message = sanitized_error(RuntimeError("https://x.test/a?apiKey=secret token=abc"))
    assert "secret" not in message and "abc" not in message and "https" not in message

def test_partial_kalshi_failure_still_persists_hardrock():
    report, _, store, kalshi, _ = run_scheduler([event("15m")])
    assert report["completed"] and store.observations
    assert not any(name.startswith(("place_", "submit_", "execute_")) for name in dir(SnapshotScheduler))

def test_failed_slot_report_is_categorized_costed_sanitized_and_persisted():
    odds, store, kalshi = Odds([event("24h", "paid-event")]), Store(), Kalshi()
    scheduler = SnapshotScheduler(odds=odds, store=store, kalshi=kalshi, load_model=lambda: object(),
        build_observations=lambda *_: (_ for _ in ()).throw(
            ValueError("stable player identity mismatch token=super-secret https://private.invalid")), now=lambda: NOW)
    report = asyncio.run(scheduler.run())
    assert len(report["failed"]) == 1 and not report["completed"]
    failure = report["failed"][0]
    assert failure == {**failure, "provider_event_id": "paid-event", "matchup": "Buffalo Bills at Miami Dolphins",
        "failure_category": "player_identity_mismatch", "provider_request_made": True,
        "estimated_credit_cost": 3, "actual_credit_cost": 3, "retryable": True}
    assert "super-secret" not in failure["reason"] and "private.invalid" not in failure["reason"]
    assert store.states[("paid-event", "24h", NOW)]["status"] == "failed"
    assert store.executions[0]["details"]["failed"] == [failure]

def test_failed_slot_retries_paid_request_until_budget_then_stops():
    odds, store, kalshi = Odds([event("24h")]), Store(), Kalshi()
    scheduler = SnapshotScheduler(odds=odds, store=store, kalshi=kalshi, load_model=lambda: object(),
        build_observations=lambda *_: [], config=SchedulerConfig(retry_budget=2), now=lambda: NOW)
    first = asyncio.run(scheduler.run())
    second = asyncio.run(scheduler.run())
    third = asyncio.run(scheduler.run())
    assert first["failed"][0]["retryable"] is True
    assert second["failed"][0]["retryable"] is False
    assert not third["due"] and not third["selected"]
    assert odds.targeted == ["g", "g"] and odds.usage["quota_consumed"] == 6
    assert store.states[("g", "24h", NOW)]["attempts"] == 2

def test_successful_slot_is_idempotent_and_does_not_repeat_paid_request():
    odds, store, kalshi = Odds([event("24h")]), Store(), Kalshi()
    scheduler = SnapshotScheduler(odds=odds, store=store, kalshi=kalshi, load_model=lambda: object(),
        build_observations=lambda *_: [{"row": 1}], now=lambda: NOW)
    assert asyncio.run(scheduler.run())["completed"]
    assert not asyncio.run(scheduler.run())["selected"]
    assert odds.targeted == ["g"] and odds.usage["quota_consumed"] == 3
