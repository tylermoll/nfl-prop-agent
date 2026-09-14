"""Print the read-only settlement identity audit; never writes settlements."""
from __future__ import annotations

import json

from app.config import settings
from app.historical.ingestion import NflverseClient
from app.settlement_residual_audit import audit_settlement_identities_with_residual_bijection
from app.shadow_storage import ShadowStore


def run_audit() -> dict:
    return audit_settlement_identities_with_residual_bijection(
        store=ShadowStore(settings.database_url),
        nflverse=NflverseClient(settings.settlement_cache_dir),
        refresh=settings.settlement_nflverse_refresh,
    )


def main() -> int:
    print(json.dumps(run_audit(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
