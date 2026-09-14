"""Read-only settled-slate exports and descriptive research summaries."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timezone
from statistics import mean
from typing import Any, Iterable

from sqlalchemy import and_, select

from app.pregame_context import evaluate_context
from app.shadow_storage import observations, pregame_context_snapshots, settlements

CAPTURE_TARGETS = {"24h": 24 * 3600, "6h": 6 * 3600, "90m": 90 * 60, "15m": 15 * 60}
EDGE_BUCKETS = ((2, "<2pp"), (5, "2–5pp"), (8, "5–8pp"), (15, "8–15pp"),
                (25, "15–25pp"), (35, "25–35pp"), (float("inf"), "35pp+"))
PROBABILITY_BUCKETS = ((.55, "50–55%"), (.60, "55–60%"), (.65, "60–65%"),
                       (.70, "65–70%"), (.75, "70–75%"), (.80, "75–80%"),
                       (.85, "80–85%"), (.90, "85–90%"), (float("inf"), "90%+"))


def edge_bucket(edge_pp: float) -> str:
    value = abs(float(edge_pp))
    return next(label for upper, label in EDGE_BUCKETS if value < upper)


def probability_bucket(probability: float) -> str:
    value = float(probability)
    if value < .50:
        return "<50%"
    return next(label for upper, label in PROBABILITY_BUCKETS if value < upper)


def confirmation_status(row: dict) -> str:
    flags = row.get("confirmation_flags") or {}
    if flags.get("both_disagree") or flags.get("cross_market_conflict"):
        return "conflicted"
    if flags.get("both_agree") or flags.get("reference_confirmed"):
        return "confirmed"
    return "insufficient" if not _reference_count(row) else "available"


def _reference_count(row: dict) -> int | None:
    ref = row.get("reference_context") or {}
    value = ref.get("reference_book_count")
    if value is not None:
        return int(value)
    books = ref.get("books")
    return len(books) if isinstance(books, list) else None


def canonical_captures(rows: Iterable[dict]) -> dict[tuple, dict[str, dict]]:
    """Pick the pregame observation nearest each target for each exact prop side.

    Lines are retained on the selected observation and never form a join key
    across sides; this permits genuine line movement without conflating props.
    """
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        observed, kickoff = _utc(row["observed_at_utc"]), _utc(row["kickoff_utc"])
        if observed >= kickoff:
            continue
        identity = (row["game_id"], row.get("player_id") or row["player_name"],
                    row["canonical_market"], row["side"])
        grouped[identity].append(row)
    output = {}
    for identity, items in grouped.items():
        selected = {}
        for slot, target in CAPTURE_TARGETS.items():
            candidates = [r for r in items if (r.get("context") or {}).get("capture_slot") == slot]
            if candidates:
                selected[slot] = min(candidates, key=lambda r: (
                    abs((_utc(r["kickoff_utc"])-_utc(r["observed_at_utc"])).total_seconds()-target),
                    _utc(r["observed_at_utc"]), r["observation_id"]))
        output[identity] = selected
    return output


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _capture(row: dict | None) -> dict | None:
    if row is None:
        return None
    ref, kalshi = row.get("reference_context") or {}, row.get("kalshi_context") or {}
    bid, ask = kalshi.get("yes_bid"), kalshi.get("yes_ask")
    midpoint = kalshi.get("midpoint")
    if midpoint is None and isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
        midpoint = (bid + ask) / 2
    return {"observation_id": row["observation_id"], "captured_at_utc": _utc(row["observed_at_utc"]),
            "line": row.get("line"), "american_price": row.get("hard_rock_offered_odds"),
            "model_point": row.get("point_prediction"), "model_probability": row.get("model_probability"),
            "break_even_probability": row.get("offered_price_break_even_probability"),
            "probability_edge_pp": row.get("raw_probability_edge_pp"),
            "reference_book_count": _reference_count(row),
            "same_threshold_reference_probability": ref.get("exact_threshold_over_no_vig_probability"),
            "kalshi_bid": bid, "kalshi_ask": ask, "kalshi_midpoint": midpoint,
            "confirmation_status": confirmation_status(row)}


def build_export_rows(raw_rows: list[dict], contexts: list[dict] | None = None) -> list[dict]:
    """Create one wide record per event/player/market/side from persisted settlements."""
    settled = [r for r in raw_rows if r.get("settled_at_utc") is not None]
    captures = canonical_captures(settled)
    context_by_observation: dict[str, list[dict]] = defaultdict(list)
    for item in contexts or []:
        context_by_observation[item["observation_id"]].append(item)
    result = []
    for identity, slots in sorted(captures.items(), key=lambda item: tuple(str(x) for x in item[0])):
        if not slots:
            continue
        anchor = max(slots.values(), key=lambda r: _utc(r["observed_at_utc"]))
        all_identity = [r for r in settled if (r["game_id"], r.get("player_id") or r["player_name"],
                        r["canonical_market"], r["side"]) == identity]
        # The row-level offered line/price and outcome must all come from the
        # same immutable observation/settlement pair.
        final = anchor
        snapshots = [c for r in all_identity for c in context_by_observation.get(r["observation_id"], [])
                     if _utc(c["as_of_utc"]) < _utc(anchor["kickoff_utc"])]
        snapshot = max(snapshots, key=lambda c: _utc(c["as_of_utc"])) if snapshots else None
        view = {"probability_edge_pp": anchor.get("raw_probability_edge_pp"),
                "reference_book_coverage": _reference_count(anchor),
                "same_threshold_reference_probability": (anchor.get("reference_context") or {}).get(
                    "exact_threshold_over_no_vig_probability"), "model_probability": anchor.get("model_probability"),
                "side": anchor["side"], "canonical_market": anchor["canonical_market"]}
        overlay = evaluate_context(view, snapshot, now=_utc(anchor["kickoff_utc"]))
        row = {"event_id": anchor["game_id"], "player_id": anchor.get("player_id"),
               "player_name": anchor["player_name"], "team": anchor.get("team"),
               "opponent": anchor.get("opponent"), "home_away": (anchor.get("context") or {}).get("home_away"),
               "kickoff_utc": _utc(anchor["kickoff_utc"]), "canonical_market": anchor["canonical_market"],
               "side": anchor["side"], "sportsbook_line": anchor.get("line"),
               "sportsbook_american_price": anchor.get("hard_rock_offered_odds"),
               **{f"capture_{slot}": _capture(slots.get(slot)) for slot in CAPTURE_TARGETS},
               "pregame_context": {"as_of_utc": overlay.get("as_of_utc"),
                   "weather_status": overlay["dimensions"]["weather_context"]["label"],
                   "injury_status": overlay["dimensions"]["injury_context"]["label"],
                   "role_status": overlay["dimensions"]["role_context"]["label"],
                   "evidence_quality": overlay["evidence_quality"]},
               "actual_result": final.get("actual_value"), "outcome": final.get("result"),
               "flat_10_pnl": final.get("fixed_unit_profit_loss"),
               "american_odds_payout_used": (final.get("american_odds") if final.get("american_odds") is not None
                                                else final.get("hard_rock_offered_odds")),
               "settled_at_utc": _utc(final["settled_at_utc"])}
        result.append(row)
    return result


def performance_summary(rows: Iterable[dict], group_fields: tuple[str, ...] = ()) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[tuple(row.get(field) for field in group_fields)].append(row)
    output = []
    for key, items in groups.items():
        decisive = [r for r in items if r.get("result") in {"win", "loss"}]
        stake = sum(float(r.get("fixed_unit") or 0) for r in items)
        record = {field: value for field, value in zip(group_fields, key)}
        record.update({"bets": len(items), "wins": sum(r.get("result") == "win" for r in items),
            "losses": sum(r.get("result") == "loss" for r in items),
            "pushes": sum(r.get("result") == "push" for r in items),
            "hit_rate": sum(r.get("result") == "win" for r in decisive) / len(decisive) if decisive else None,
            "flat_10_pnl": sum(float(r.get("fixed_unit_profit_loss") or 0) for r in items),
            "roi": sum(float(r.get("fixed_unit_profit_loss") or 0) for r in items) / stake if stake else None,
            "average_model_probability": _average(items, "model_probability"),
            "average_break_even_probability": _average(items, "offered_price_break_even_probability"),
            "average_model_edge_pp": _average(items, "raw_probability_edge_pp")})
        output.append(record)
    return output


def calibration_table(rows: Iterable[dict]) -> list[dict]:
    material = [dict(row, probability_bucket=probability_bucket(row["model_probability"])) for row in rows
                if row.get("model_probability") is not None and row.get("result") in {"win", "loss"}]
    return [{"probability_bucket": item["probability_bucket"], "observations": item["bets"],
             "average_predicted_probability": item["average_model_probability"], "realized_hit_rate": item["hit_rate"]}
            for item in performance_summary(material, ("probability_bucket",))]


def _average(rows: list[dict], field: str) -> float | None:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return mean(values) if values else None


class SettledResearchRepository:
    def __init__(self, engine):
        self.engine = engine

    def slate(self, slate_date: date) -> list[dict]:
        start = datetime.combine(slate_date, time.min, timezone.utc)
        end = datetime.combine(slate_date, time.max, timezone.utc)
        joined = observations.join(settlements, observations.c.observation_id == settlements.c.observation_id)
        with self.engine.connect() as connection:
            raw = [dict(r._mapping) for r in connection.execute(select(
                observations, *[c for c in settlements.c if c.name not in {"observation_id", "line", "side"}]
            ).select_from(joined).where(and_(observations.c.kickoff_utc >= start,
                                             observations.c.kickoff_utc <= end)))]
            ids = [row["observation_id"] for row in raw]
            contexts = ([dict(r._mapping) for r in connection.execute(select(pregame_context_snapshots).where(
                pregame_context_snapshots.c.observation_id.in_(ids)))] if ids else [])
        return build_export_rows(raw, contexts)
