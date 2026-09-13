"""Append public pregame context for today's existing NFL observations."""
from __future__ import annotations

import json
import os

from app.config import settings
from app.pregame_context_collector import PublicContextClient, collect_pregame_context
from app.shadow_storage import ShadowStore


def run_pregame_context_cycle() -> dict:
    agent = os.getenv("NWS_USER_AGENT", "nfl-prop-agent/1.0 (public pregame context collector)")
    return collect_pregame_context(ShadowStore(settings.database_url), PublicContextClient(agent))


def main() -> int:
    print(json.dumps(run_pregame_context_cycle(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
