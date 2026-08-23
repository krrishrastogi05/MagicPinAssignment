from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.composer import build_fact_ledger, build_message_plan, validate_composition
from tests.conftest import push, push_all


def test_health_and_metadata_contract(client: TestClient):
    health = client.get("/v1/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["contexts_loaded"] == {
        "category": 0,
        "merchant": 0,
        "customer": 0,
        "trigger": 0,
    }

    metadata = client.get("/v1/metadata")
    assert metadata.status_code == 200
    assert metadata.json()["team_name"] == "Test Team"
    assert metadata.json()["model"] == "deterministic-fallback"


def test_context_versioning_is_monotonic(client: TestClient):
    first = push(client, "category", "dentists", {"slug": "dentists"}, 1)
    assert first.status_code == 200
    assert first.json()["accepted"] is True

    # Identical replay of the same version is an idempotent no-op success:
    # the judge harness re-pushes its base contexts on every warmup.
    idempotent = push(client, "category", "dentists", {"slug": "dentists"}, 1)
    assert idempotent.status_code == 200
    assert idempotent.json()["accepted"] is True

    # Same version with a *different* payload is a genuine conflict.
    replay = push(client, "category", "dentists", {"slug": "wrong"}, 1)
    assert replay.status_code == 409
    assert replay.json()["current_version"] == 1

    stale = push(client, "category", "dentists", {"slug": "wrong"}, 0)
    assert stale.status_code == 409

    update = push(client, "category", "dentists", {"slug": "dentists", "x": 2}, 2)
    assert update.status_code == 200
    assert client.get("/v1/healthz").json()["contexts_loaded"]["category"] == 1


def test_context_size_and_schema_limits(client: TestClient):
    oversized = push(client, "merchant", "large", {"blob": "x" * (501 * 1024)})
    assert oversized.status_code == 413

    invalid = client.post(
        "/v1/context",
        json={
            "scope": "merchant",
            "context_id": "m",
            "version": 1,
            "payload": {},
            "delivered_at": "not-a-date",
            "unexpected": True,
        },
    )
    assert invalid.status_code == 422


def test_concurrent_context_updates_preserve_highest_version(client: TestClient):
    def write(version: int):
        return push(client, "merchant", "m-race", {"version": version}, version)

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(write, range(1, 21)))
    assert any(response.status_code == 200 for response in responses)
    stored = client.app.state.store.get_context("merchant", "m-race")
    assert stored is not None
    assert stored.version == 20
    assert stored.payload == {"version": 20}


def test_unknown_triggers_are_safe_abstentions(client: TestClient):
    response = client.post(
        "/v1/tick",
        json={"now": "2026-08-23T10:00:00Z", "available_triggers": ["missing"]},
    )
    assert response.status_code == 200
    assert response.json() == {"actions": []}


def test_tick_is_grounded_deduplicated_and_deterministic(
    client: TestClient, dataset
):
    push_all(client, dataset, "category", "merchant", "trigger")
    trigger_id = "trg_001_research_digest_dentists"
    request = {
        "now": "2026-08-23T10:00:00Z",
        "available_triggers": [trigger_id],
    }
    first = client.post("/v1/tick", json=request)
    assert first.status_code == 200
    assert first.headers["x-vera-composer"] == "deterministic-fallback"
    assert first.headers["x-vera-composer-detail"] == "agent-unavailable"
    actions = first.json()["actions"]
    assert len(actions) == 1
    action = actions[0]
    assert action["merchant_id"] == "m_001_drmeera_dentist_delhi"
    assert action["trigger_id"] == trigger_id
    assert action["send_as"] == "vera"
    assert "Dr. Meera" in action["body"]
    assert "JIDA" in action["body"] or "fluoride" in action["body"].lower()
    assert "http" not in action["body"].lower()
    assert action["suppression_key"] == "research:dentists:2026-W17"
    assert "composer_source" not in action
    assert "composer_detail" not in action

    repeated = client.post("/v1/tick", json=request)
    assert repeated.status_code == 200
    assert repeated.headers["x-vera-composer"] == "none"
    assert repeated.headers["x-vera-composer-detail"] == "none"
    assert repeated.json() == {"actions": []}


def test_probe_contexts_do_not_pollute_evaluator_health_counts(client: TestClient):
    assert push(client, "category", "preflight", {"slug": "preflight"}).status_code == 200
    assert push(
        client,
        "merchant",
        "preflight_merchant_123",
        {"merchant_id": "preflight_merchant_123"},
    ).status_code == 200
    assert push(
        client,
        "trigger",
        "__vera_probe__123_trigger",
        {"id": "__vera_probe__123_trigger"},
    ).status_code == 200

    assert client.get("/v1/healthz").json()["contexts_loaded"] == {
        "category": 0,
        "merchant": 0,
        "customer": 0,
        "trigger": 0,
    }


def test_customer_trigger_requires_context_and_consent(client: TestClient, dataset):
    trigger_id = "trg_003_recall_due_priya"
    push_all(client, dataset, "category", "merchant", "trigger")
    without_customer = client.post(
        "/v1/tick",
        json={"now": "2026-08-23T10:00:00Z", "available_triggers": [trigger_id]},
    )
    assert without_customer.json() == {"actions": []}

    customer_id = "c_001_priya_for_m001"
    assert push(client, "customer", customer_id, dataset["customer"][customer_id]).status_code == 200
    with_customer = client.post(
        "/v1/tick",
        json={"now": "2026-08-23T10:00:00Z", "available_triggers": [trigger_id]},
    )
    action = with_customer.json()["actions"][0]
    assert action["customer_id"] == customer_id
    assert action["send_as"] == "merchant_on_behalf"
    assert "Priya" in action["body"]


def test_one_action_per_recipient_and_hard_cap(client: TestClient, dataset):
    push_all(client, dataset, "category", "merchant", "customer", "trigger")
    trigger_ids = list(dataset["trigger"])
    response = client.post(
        "/v1/tick",
        json={"now": "2026-08-23T10:00:00Z", "available_triggers": trigger_ids},
    )
    actions = response.json()["actions"]
    assert len(actions) <= 20
    recipients = [action.get("customer_id") or action["merchant_id"] for action in actions]
    assert len(recipients) == len(set(recipients))


def test_parallel_ticks_cannot_double_send(client: TestClient, dataset):
    push_all(client, dataset, "category", "merchant", "trigger")
    request = {
        "now": "2026-08-23T10:00:00Z",
        "available_triggers": ["trg_002_compliance_dci_radiograph"],
    }
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: client.post("/v1/tick", json=request), range(10)))
    assert sum(len(result.json()["actions"]) for result in results) == 1


def test_independent_recipient_compositions_run_concurrently(
    client: TestClient, dataset, monkeypatch
):
    push_all(client, dataset, "category", "merchant", "trigger")
    original_compose = client.app.state.composer.compose
    active = 0
    max_active = 0

    async def delayed_compose(candidate, conversation_id, previous_body=None):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.05)
            return await original_compose(candidate, conversation_id, previous_body)
        finally:
            active -= 1

    monkeypatch.setattr(client.app.state.composer, "compose", delayed_compose)
    response = client.post(
        "/v1/tick",
        json={
            "now": "2026-08-23T10:00:00Z",
            "available_triggers": [
                "trg_001_research_digest_dentists",
                "trg_004_perf_dip_bharat",
            ],
        },
    )

    assert response.status_code == 200
    assert len(response.json()["actions"]) == 2
    assert max_active == 2


def test_every_seed_message_plan_passes_grounding_validator(client: TestClient, dataset):
    push_all(client, dataset, "category", "merchant", "customer", "trigger")
    failures = {}
    now = "2026-08-23T10:00:00Z"
    for trigger_id in dataset["trigger"]:
        trigger = client.app.state.store.get_context("trigger", trigger_id)
        candidate = client.app.state.policy.build_candidate(trigger, now)
        if candidate is None:
            continue
        ledger = build_fact_ledger(candidate)
        plan = build_message_plan(candidate, ledger)
        errors = validate_composition(
            body=plan.fallback_body,
            cta=plan.brief.cta,
            used_fact_ids=plan.used_fact_ids,
            ledger=ledger,
            category=candidate.category_payload,
            expected_cta=plan.brief.cta,
            previous_body=None,
            no_urls=True,
        )
        if errors:
            failures[trigger_id] = errors
    assert failures == {}
