from __future__ import annotations

from pathlib import Path

from app.composer import Composer
from app.config import Settings
from app.storage import Store


def test_pydantic_ai_structured_composer_can_be_plugged_in(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "framework.db",
        model_enabled=True,
        model_names=("test",),
    )
    store = Store(settings.database_path)
    store.initialize()
    composer = Composer(store, settings)
    assert composer._agent is not None
    assert composer.model_label == "test"
