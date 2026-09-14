"""Export an already-settled UTC kickoff slate without calling providers."""
from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

from app.config import settings
from app.research_export import SettledResearchRepository
from app.shadow_storage import normalize_database_url


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def flatten(row: dict, prefix: str = "") -> dict:
    output = {}
    for key, value in row.items():
        name = f"{prefix}_{key}" if prefix else key
        if isinstance(value, dict):
            output.update(flatten(value, name))
        else:
            output[name] = value.isoformat() if isinstance(value, (date, datetime)) else value
    return output


def write_export(rows: list[dict], output: Path, fmt: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        output.write_text(json.dumps(rows, default=_json_default, indent=2) + "\n")
        return
    flattened = [flatten(row) for row in rows]
    fields = list(dict.fromkeys(key for row in flattened for key in row))
    with output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(flattened)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="UTC kickoff date (YYYY-MM-DD)")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--format", choices=("csv", "json"), help="defaults from output extension")
    args = parser.parse_args()
    fmt = args.format or ("json" if args.output.suffix.lower() == ".json" else "csv")
    engine = create_engine(normalize_database_url(settings.database_url))
    write_export(SettledResearchRepository(engine).slate(args.date), args.output, fmt)


if __name__ == "__main__":
    main()
