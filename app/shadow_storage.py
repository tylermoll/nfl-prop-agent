"""Append-only SQL storage for prospective paper observations and settlements."""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import JSON, Boolean, Column, DateTime, Float, Integer, MetaData, String, Table, UniqueConstraint, create_engine, insert, select, update
from app.math_utils import american_to_decimal

metadata = MetaData()
observations = Table("shadow_observations", metadata,
    Column("observation_id", String(36), primary_key=True), Column("observed_at_utc", DateTime(timezone=True), nullable=False),
    Column("kickoff_utc", DateTime(timezone=True), nullable=False), Column("game_id", String, nullable=False),
    Column("player_id", String), Column("player_name", String, nullable=False), Column("team", String), Column("opponent", String),
    Column("canonical_market", String, nullable=False), Column("line", Float, nullable=False), Column("side", String, nullable=False),
    Column("hard_rock_offered_odds", Integer, nullable=False), Column("offered_price_break_even_probability", Float, nullable=False),
    Column("model_probability", Float, nullable=False), Column("model_version", String, nullable=False),
    Column("raw_probability_edge_pp", Float, nullable=False), Column("hypothetical_expected_return_per_dollar", Float, nullable=False),
    Column("edge_bucket", String, nullable=False), Column("feature_built_at_utc", DateTime(timezone=True), nullable=False),
    Column("uncertainty_method", String, nullable=False), Column("uncertainty_version", String, nullable=False),
    Column("residual_bucket", Integer), Column("uncertainty_scale", Float), Column("point_prediction", Float),
    Column("reference_context", JSON, nullable=False), Column("kalshi_context", JSON, nullable=False),
    Column("source_observation_ids", JSON, nullable=False), Column("freshness", JSON, nullable=False),
    Column("context", JSON, nullable=False), Column("confirmation_flags", JSON, nullable=False), Column("research_config", JSON, nullable=False))
settlements = Table("shadow_settlements", metadata,
    Column("observation_id", String(36), primary_key=True), Column("settled_at_utc", DateTime(timezone=True), nullable=False),
    Column("actual_value", Float, nullable=False), Column("result", String, nullable=False),
    Column("profit_loss_per_dollar", Float, nullable=False), Column("fixed_unit", Float, nullable=False),
    Column("fixed_unit_profit_loss", Float, nullable=False), Column("result_source_id", String))
scheduler_slots = Table("shadow_capture_slots", metadata,
    Column("event_id", String, nullable=False), Column("slot", String, nullable=False),
    Column("target_time_utc", DateTime(timezone=True), nullable=False), Column("status", String, nullable=False),
    Column("attempts", Integer, nullable=False, default=0), Column("reason", String),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint("event_id", "slot", "target_time_utc", name="uq_shadow_capture_slot"))
scheduler_executions = Table("shadow_scheduler_executions", metadata,
    Column("execution_id", String(36), primary_key=True), Column("started_at_utc", DateTime(timezone=True), nullable=False),
    Column("ended_at_utc", DateTime(timezone=True)), Column("details", JSON, nullable=False))

class ShadowStore:
    def __init__(self, database_url: str):
        self.engine = create_engine(database_url)
        metadata.create_all(self.engine)
    def append(self, row: dict) -> None:
        allowed = {c.name for c in observations.columns}
        with self.engine.begin() as connection:
            connection.execute(insert(observations), {k: v for k, v in row.items() if k in allowed})
    def complete_capture(self, rows: list[dict], slot_row: dict) -> None:
        """Atomically append an immutable capture and mark its slot complete."""
        allowed = {c.name for c in observations.columns}
        with self.engine.begin() as connection:
            for row in rows:
                connection.execute(insert(observations), {k: v for k, v in row.items() if k in allowed})
            existing = connection.execute(select(scheduler_slots).where(
                scheduler_slots.c.event_id == slot_row["event_id"],
                scheduler_slots.c.slot == slot_row["slot"],
                scheduler_slots.c.target_time_utc == slot_row["target_time_utc"])).first()
            if existing:
                connection.execute(update(scheduler_slots).where(
                    scheduler_slots.c.event_id == slot_row["event_id"],
                    scheduler_slots.c.slot == slot_row["slot"],
                    scheduler_slots.c.target_time_utc == slot_row["target_time_utc"]).values(**slot_row))
            else:
                connection.execute(insert(scheduler_slots), slot_row)
    def all(self) -> list[dict]:
        with self.engine.connect() as connection:
            return [dict(r._mapping) for r in connection.execute(select(observations).order_by(observations.c.observed_at_utc))]
    def timeline(self, game_id: str, player_id: str | None, player_name: str, market: str, line: float, side: str) -> dict:
        rows = [r for r in self.all() if r["game_id"] == game_id and r["player_id"] == player_id and
                r["player_name"] == player_name and r["canonical_market"] == market and r["line"] == line and r["side"] == side]
        valid = [r for r in rows if r["observed_at_utc"] < r["kickoff_utc"]]
        if not valid: return {"first": None, "latest": None, "best_price": None, "final_pregame": None}
        # Maximum decimal payout is best for a hypothetical entrant.
        best = max(valid, key=lambda r: american_to_decimal(r["hard_rock_offered_odds"]))
        return {"first": valid[0], "latest": valid[-1], "best_price": best, "final_pregame": valid[-1]}
    def closing_series(self, game_id: str, player_id: str | None, player_name: str,
                       market: str, side: str) -> dict:
        """Return first/final quote across lines plus same-line price comparison."""
        rows = [r for r in self.all() if r["game_id"] == game_id and r["player_id"] == player_id and
                r["player_name"] == player_name and r["canonical_market"] == market and r["side"] == side and
                r["observed_at_utc"] < r["kickoff_utc"]]
        if not rows: return {"first": None, "final_pregame": None, "line_movement": None, "same_line_price_movement": None}
        first, final = rows[0], rows[-1]
        same_line = [r for r in rows if r["line"] == first["line"]]
        return {"first": first, "final_pregame": final, "line_movement": final["line"]-first["line"],
                "same_line_price_movement": (same_line[-1]["hard_rock_offered_odds"]-first["hard_rock_offered_odds"]
                                             if len(same_line) > 1 else None),
                "entry_beat_final_same_line_price": (american_to_decimal(first["hard_rock_offered_odds"]) >
                    american_to_decimal(same_line[-1]["hard_rock_offered_odds"]) if len(same_line) > 1 else None)}
    def record_settlement(self, row: dict) -> None:
        with self.engine.begin() as connection: connection.execute(insert(settlements), row)

    def slot_state(self, event_id: str, slot: str, target_time: datetime) -> dict | None:
        with self.engine.connect() as c:
            row = c.execute(select(scheduler_slots).where(scheduler_slots.c.event_id == event_id,
                scheduler_slots.c.slot == slot, scheduler_slots.c.target_time_utc == target_time)).first()
            return dict(row._mapping) if row else None

    def record_slot(self, row: dict) -> None:
        """Upsert mutable attempt metadata; market observations remain append-only."""
        existing = self.slot_state(row["event_id"], row["slot"], row["target_time_utc"])
        with self.engine.begin() as c:
            if existing:
                c.execute(update(scheduler_slots).where(scheduler_slots.c.event_id == row["event_id"],
                    scheduler_slots.c.slot == row["slot"], scheduler_slots.c.target_time_utc == row["target_time_utc"]).values(**row))
            else: c.execute(insert(scheduler_slots), row)

    def record_execution(self, row: dict) -> None:
        with self.engine.begin() as c: c.execute(insert(scheduler_executions), row)
