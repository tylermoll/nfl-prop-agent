"""CLI for the production current-feature materialization job."""
import argparse
import json
from dataclasses import asdict

from app.current_features import build_current_features


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    _, report = build_current_features(args.output, dry_run=args.dry_run)
    print(json.dumps(asdict(report), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
