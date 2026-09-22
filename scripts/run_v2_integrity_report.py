"""CLI for the strictly read-only prospective V2 integrity report."""
from __future__ import annotations

import argparse
import json

from sqlalchemy import create_engine

from app.config import settings
from app.shadow_storage import normalize_database_url
from app.v2_integrity import load_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit persisted canonical V2.0 decisions (SELECT queries only).")
    parser.add_argument("--event-id", help="restrict to a provider event identity")
    parser.add_argument("--execution-id", help="restrict using persisted scheduler execution metadata")
    parser.add_argument("--compact", action="store_true", help="emit compact machine-readable JSON")
    args = parser.parse_args()
    engine = create_engine(normalize_database_url(settings.database_url))
    report = load_report(engine, event_id=args.event_id, execution_id=args.execution_id)
    print(json.dumps(report, default=str, sort_keys=True, indent=None if args.compact else 2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
