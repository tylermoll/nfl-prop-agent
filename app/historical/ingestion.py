"""Cached, auditable downloads of maintained nflverse parquet releases."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from .catalog import DATASETS, DatasetSpec


class NflverseClient:
    """Download immutable local copies and retain a sidecar provenance manifest."""

    def __init__(self, cache_dir: str | Path = "data/historical/raw", timeout: int = 90):
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout

    def fetch(self, dataset: str, season: int | None = None, *, refresh: bool = False) -> pd.DataFrame:
        spec = DATASETS[dataset]
        if spec.partitioned and season is None:
            raise ValueError(f"{dataset} requires a season")
        if season is not None and season < spec.season_min:
            raise ValueError(f"{dataset} is not cataloged before {spec.season_min}")
        token = str(season) if spec.partitioned else "all"
        target = self.cache_dir / dataset / f"{token}.parquet"
        if refresh or not target.exists():
            self._download(spec, season, target)
        return pd.read_parquet(target)

    def _download(self, spec: DatasetSpec, season: int | None, target: Path) -> None:
        url = spec.url_template.format(season=season)
        request = Request(url, headers={"User-Agent": "nfl-prop-agent/1.0"})
        retrieved = datetime.now(timezone.utc).isoformat()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".part")
        digest = hashlib.sha256()
        with urlopen(request, timeout=self.timeout) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
            headers = dict(response.headers.items())
        temporary.replace(target)
        frame = pd.read_parquet(target)
        metadata = {
            "dataset": spec.name,
            "source": "nflverse/nflverse-data",
            "retrieval_method": "HTTPS GitHub release parquet",
            "source_url": url,
            "season": season,
            "cataloged_season_coverage": [spec.season_min, spec.season_max or "current release"],
            "retrieved_at_utc": retrieved,
            "etag": headers.get("ETag"),
            "last_modified": headers.get("Last-Modified"),
            "sha256": digest.hexdigest(),
            "bytes": target.stat().st_size,
            "schema": {column: str(dtype) for column, dtype in frame.dtypes.items()},
            "rows": len(frame),
            "known_limitations": spec.limitation,
        }
        target.with_suffix(".metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))
