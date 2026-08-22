from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "dataset"


def read_dataset() -> dict[str, dict[str, dict[str, Any]]]:
    categories: dict[str, dict[str, Any]] = {}
    for path in (DATASET / "categories").glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        categories[payload["slug"]] = payload

    result: dict[str, dict[str, dict[str, Any]]] = {"category": categories}
    for filename, scope, container, key in (
        ("merchants_seed.json", "merchant", "merchants", "merchant_id"),
        ("customers_seed.json", "customer", "customers", "customer_id"),
        ("triggers_seed.json", "trigger", "triggers", "id"),
    ):
        data = json.loads((DATASET / filename).read_text(encoding="utf-8"))
        result[scope] = {item[key]: item for item in data[container]}
    return result


@pytest.fixture
def dataset() -> dict[str, dict[str, dict[str, Any]]]:
    return read_dataset()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_path=tmp_path / "vera-test.db",
        model_enabled=False,
        model_names=(),
        team_name="Test Team",
        team_members=("Test Member",),
        contact_email="test@example.com",
        submitted_at="2026-08-23T00:00:00Z",
    )
    return TestClient(create_app(settings))


def push(
    client: TestClient,
    scope: str,
    context_id: str,
    payload: dict[str, Any],
    version: int = 1,
):
    return client.post(
        "/v1/context",
        json={
            "scope": scope,
            "context_id": context_id,
            "version": version,
            "payload": payload,
            "delivered_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        },
    )


def push_all(
    client: TestClient,
    dataset: dict[str, dict[str, dict[str, Any]]],
    *scopes: str,
) -> None:
    for scope in scopes:
        for context_id, payload in dataset[scope].items():
            response = push(client, scope, context_id, payload)
            assert response.status_code == 200, response.text
