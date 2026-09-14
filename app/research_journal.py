"""Append-only discretionary decision journal, independent of model execution."""
from __future__ import annotations
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select
from app.shadow_storage import ShadowStore, selection_journal

REASON_CODES = frozenset({"model_edge", "market_confirmation", "role_confidence", "injury_context",
    "weather_context", "matchup", "projection_plausibility", "cross_market_conflict",
    "insufficient_reference_coverage", "manual_conviction"})

class SelectionJournal:
    def __init__(self, store: ShadowStore): self.store = store

    def append(self, observation_id: str, *, selected: bool, intended_stake: float | None = None,
               reason_codes: list[str] | None = None, note: str | None = None,
               selected_at_utc: datetime | None = None, journal_id: str | None = None) -> dict:
        codes = list(reason_codes or [])
        unknown = set(codes) - REASON_CODES
        if unknown: raise ValueError(f"unknown reason codes: {sorted(unknown)}")
        if intended_stake is not None and intended_stake < 0: raise ValueError("intended stake cannot be negative")
        row = {"journal_id": journal_id or str(uuid4()), "observation_id": observation_id,
               "selected": bool(selected), "intended_stake": intended_stake,
               "selected_at_utc": selected_at_utc or datetime.now(timezone.utc),
               "reason_codes": codes, "note": note}
        self.store.append_selection(row)
        return row

    def all(self) -> list[dict]:
        with self.store.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(select(selection_journal).order_by(
                selection_journal.c.selected_at_utc, selection_journal.c.journal_id))]
