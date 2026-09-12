import asyncio
import logging
import sys

import pytest
from sqlalchemy import create_engine as sqlalchemy_create_engine

from app.shadow_storage import ShadowStore, normalize_database_url


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "postgresql://railway:secret@postgres.railway.internal:5432/railway",
            "postgresql+psycopg://railway:secret@postgres.railway.internal:5432/railway",
        ),
        (
            "postgresql+psycopg://railway:secret@postgres.railway.internal/railway",
            "postgresql+psycopg://railway:secret@postgres.railway.internal/railway",
        ),
        ("sqlite:///local-shadow.db", "sqlite:///local-shadow.db"),
    ],
)
def test_normalize_database_url(source, expected):
    assert normalize_database_url(source) == expected


def test_shadow_store_initializes_with_normalized_url_without_logging_credentials(monkeypatch, caplog):
    supplied = "postgresql://railway_user:super-secret@postgres.railway.internal/railway"
    captured = []

    def create_test_engine(url):
        captured.append(url)
        return sqlalchemy_create_engine("sqlite:///:memory:")

    monkeypatch.setattr("app.shadow_storage.create_engine", create_test_engine)
    with caplog.at_level(logging.DEBUG):
        store = ShadowStore(supplied)

    assert captured == [
        "postgresql+psycopg://railway_user:super-secret@postgres.railway.internal/railway"
    ]
    assert store.engine.dialect.name == "sqlite"
    assert "super-secret" not in caplog.text
    assert supplied not in caplog.text


def test_scheduler_dry_run_constructs_store_with_railway_url_without_live_calls(monkeypatch, capsys):
    from scripts import run_snapshot_scheduler

    supplied = "postgresql://railway_user:super-secret@postgres.railway.internal/railway"
    captured = []

    def create_test_engine(url):
        captured.append(url)
        return sqlalchemy_create_engine("sqlite:///:memory:")

    class OfflineOdds:
        def __init__(self):
            self.usage = {"requests_remaining": 100, "http_requests": 0, "quota_consumed": 0}

        async def discover_events(self):
            return []

    class OfflineKalshi:
        def __init__(self, **_kwargs):
            pass

        async def fetch_nfl_player_props(self):
            pytest.fail("dry-run must not call Kalshi")

    monkeypatch.setattr("app.shadow_storage.create_engine", create_test_engine)
    monkeypatch.setattr(run_snapshot_scheduler.settings, "database_url", supplied)
    monkeypatch.setattr(run_snapshot_scheduler, "TheOddsApiProvider", OfflineOdds)
    monkeypatch.setattr(run_snapshot_scheduler, "KalshiProvider", OfflineKalshi)
    monkeypatch.setattr(sys, "argv", ["run_snapshot_scheduler.py", "--dry-run"])

    assert asyncio.run(run_snapshot_scheduler.main()) == 0
    output = capsys.readouterr().out
    assert captured == [
        "postgresql+psycopg://railway_user:super-secret@postgres.railway.internal/railway"
    ]
    assert '"dry_run": true' in output
    assert "super-secret" not in output
    assert supplied not in output
