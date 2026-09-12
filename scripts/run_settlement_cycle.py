"""Production CLI for nflverse-only shadow-observation settlement."""
from __future__ import annotations

import json

from app.config import settings
from app.historical.ingestion import NflverseClient
from app.settlement import SettlementCycle
from app.shadow_storage import ShadowStore


def run_settlement_cycle() -> dict:
    store = ShadowStore(settings.database_url)
    client = NflverseClient(settings.settlement_cache_dir)
    return SettlementCycle(store=store, nflverse=client, unit=settings.shadow_nominal_unit,
                           refresh=settings.settlement_nflverse_refresh).run()


def main() -> int:
    print(json.dumps(run_settlement_cycle(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
