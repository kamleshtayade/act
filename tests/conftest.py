from datetime import datetime

import pytest

from mcx_agent.config import Settings
from mcx_agent.graph import Deps, Pipeline
from mcx_agent.integrations.events import IST
from mcx_agent.store import Store

# Tuesday, inside the "morning" live window, not an EIA day.
TUESDAY_10AM = datetime(2026, 10, 6, 10, 0, tzinfo=IST)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    # Keep a developer's real .env out of the tests.
    for key in ("APP_MODE", "ANTHROPIC_API_KEY", "GOCHARTING_WEBHOOK_SECRET"):
        monkeypatch.delenv(key, raising=False)
    return Settings(
        _env_file=None,
        app_mode="mock",
        db_path=tmp_path / "log.db",
        checkpoint_db_path=tmp_path / "ckpt.db",
    )


@pytest.fixture
def store(settings):
    return Store(settings.db_path)


@pytest.fixture
def make_pipeline(settings, store):
    def _make(**overrides):
        return Pipeline(Deps(settings=settings, store=store, **overrides))

    return _make
