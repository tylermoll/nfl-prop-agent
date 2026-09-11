"""CLI for the football-only historical benchmark; performs no API calls."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from app.modeling.benchmark import BenchmarkConfig, run_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("table", type=Path, help="existing historical feature-table parquet")
    parser.add_argument("--output", type=Path, default=Path("artifacts/benchmarks/latest"))
    parser.add_argument("--validation-season", type=int)
    parser.add_argument("--test-season", type=int)
    args = parser.parse_args()
    result = run_benchmark(pd.read_parquet(args.table), args.output,
                           BenchmarkConfig(validation_season=args.validation_season, test_season=args.test_season))
    print(f"wrote {args.output} in {result['runtime_seconds']:.2f}s")


if __name__ == "__main__":
    main()
