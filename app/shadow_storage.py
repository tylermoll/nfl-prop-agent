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
    Column("actual_value", Float, nullable=False), Column("line", Float), Column("side", String),
    Column("result", String, nullable=False),
    Column("profit_loss_per_dollar", Float, nullable=False), Column("fixed_unit", Float, nullable=False),
    Column("fixed_unit_profit_loss", Float, nullable=False), Column("american_odds", Integer),
    Column("result_source_id", String))
# Deliberately separate from observations and execution.  Rows are decisions
# made by a person at a point in time and, like observations, are never updated.
selection_journal = Table("research_selection_journal", metadata,
    Column("journal_id", String(36), primary_key=True),
    Column("observation_id", String(36), nullable=False, index=True),
    Column("selected", Boolean, nullable=False), Column("intended_stake", Float),
    Column("selected_at_utc", DateTime(timezone=True), nullable=False),
    Column("reason_codes", JSON, nullable=False), Column("note", String))
scheduler_slots = Table("shadow_capture_slots", metadata,
    Column("event_id", String, nullable=False), Column("slot", String, nullable=False),
    Column("target_time_utc", DateTime(timezone=True), nullable=False), Column("status", String, nullable=False),
    Column("attempts", Integer, nullable=False, default=0), Column("reason", String),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint("event_id", "slot", "target_time_utc", name="uq_shadow_capture_slot"))
scheduler_executions = Table("shadow_scheduler_executions", metadata,
    Column("execution_id", String(36), primary_key=True), Column("started_at_utc", DateTime(timezone=True), nullable=False),
    Column("ended_at_utc", DateTime(timezone=True)), Column("details", JSON, nullable=False))
# Context is deliberately append-only and separate from model observations.  An
# external, independently scheduled collector may insert a new snapshot without
# changing the probability record it annotates.
pregame_context_snapshots = Table("pregame_context_snapshots", metadata,
    Column("context_id", String(36), primary_key=True),
    Column("observation_id", String(36), nullable=False, index=True),
    Column("as_of_utc", DateTime(timezone=True), nullable=False, index=True),
    Column("collected_at_utc", DateTime(timezone=True), nullable=False),
    Column("weather", JSON, nullable=False), Column("injuries", JSON, nullable=False),
    Column("role", JSON, nullable=False), Column("sources", JSON, nullable=False))

def normalize_database_url(database_url: str) -> str:
    """Select Psycopg 3 for driverless PostgreSQL URLs.

    Explicit SQLAlchemy driver names and non-PostgreSQL URLs are left alone.
    The URL is deliberately not logged because it may contain credentials.
    """
    railway_scheme = "postgresql://"
    if database_url.startswith(railway_scheme):
        return "postgresql+psycopg://" + database_url[len(railway_scheme):]
    return database_url


class ShadowStore:
    def __init__(self, database_url: str):
        self.engine = create_engine(normalize_database_url(database_url))
        metadata.create_all(self.engine)
    def append(self, row: dict) -> None:
        allowed = {c.name for c in observations.columns}
        with self.engine.begin() as connection:
            connection.execute(insert(observations), {k: v for k, v in row.items() if k in allowed})

    def append_context_snapshots(self, rows: list[dict]) -> int:
        """Append context rows, ignoring only already-present deterministic IDs."""
        if not rows:
            return 0
        ids = [row["context_id"] for row in rows]
        with self.engine.begin() as connection:
            existing = set(connection.execute(select(pregame_context_snapshots.c.context_id).where(
                pregame_context_snapshots.c.context_id.in_(ids))).scalars())
            # Also collapse duplicates inside a batch (for example, repeated
            # equivalent observations supplied by a caller).
            fresh_by_id = {row["context_id"]: row for row in rows if row["context_id"] not in existing}
            fresh = list(fresh_by_id.values())
            if fresh:
                if self.engine.dialect.name == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as pg_insert
                    result = connection.execute(pg_insert(pregame_context_snapshots).values(fresh)
                        .on_conflict_do_nothing(index_elements=["context_id"]))
                    return result.rowcount
                if self.engine.dialect.name == "sqlite":
                    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
                    result = connection.execute(sqlite_insert(pregame_context_snapshots).values(fresh)
                        .on_conflict_do_nothing(index_elements=["context_id"]))
                    return result.rowcount
                connection.execute(insert(pregame_context_snapshots), fresh)
        return 0
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

    def settlement_candidates(self, now: datetime) -> tuple[list[dict], int]:
        """Return passed-kickoff, unsettled observations and the settled-row count.

        The second value makes retries observable without ever loading or
        changing the settlement itself.
        """
        joined = observations.outerjoin(settlements, observations.c.observation_id == settlements.c.observation_id)
        with self.engine.connect() as connection:
            rows = connection.execute(select(observations, settlements.c.observation_id.label("settlement_id"))
                .select_from(joined).where(observations.c.kickoff_utc < now)).all()
        unsettled = [dict(row._mapping) for row in rows if row._mapping["settlement_id"] is None]
        for row in unsettled:
            row.pop("settlement_id", None)
        return unsettled, sum(row._mapping["settlement_id"] is not None for row in rows)

    def persisted_event_identity_evidence(self, event_ids: set[str]) -> dict:
        """Read scheduler metadata that may identify opaque observation events."""
        if not event_ids:
            return {"slots": [], "executions": []}
        with self.engine.connect() as connection:
            slots = [dict(row._mapping) for row in connection.execute(
                select(scheduler_slots).where(scheduler_slots.c.event_id.in_(event_ids)))]
            # Execution JSON has no relational event key. Read it without locks
            # and let the audit retain only explicitly allowlisted identity fields.
            executions = [dict(row._mapping) for row in connection.execute(select(
                scheduler_executions.c.execution_id, scheduler_executions.c.started_at_utc,
                scheduler_executions.c.ended_at_utc, scheduler_executions.c.details))]
        return {"slots": slots, "executions": executions}

    def insert_settlements(self, rows: list[dict], *, connection=None) -> None:
        """Insert a settlement batch atomically; the PK is the final race guard."""
        if connection is not None:
            connection.execute(insert(settlements), rows)
            return
        with self.engine.begin() as owned:
            owned.execute(insert(settlements), rows)

    def append_selection(self, row: dict) -> None:
        """Append a discretionary research decision; there is no update API."""
        allowed = {column.name for column in selection_journal.columns}
        with self.engine.begin() as connection:
            connection.execute(insert(selection_journal), {k: v for k, v in row.items() if k in allowed})

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
