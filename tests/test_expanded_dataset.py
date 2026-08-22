from __future__ import annotations

import random

from app.composer import build_fact_ledger, build_message_plan, validate_composition
from app.storage import utc_now
from dataset.generate_dataset import (
    SEED,
    expand_customers,
    expand_merchants,
    expand_triggers,
    load_seeds,
)
from tests.conftest import DATASET


def test_all_100_expanded_triggers_are_safe_or_abstain_cleanly(client):
    categories, merchant_seeds, customer_seeds, trigger_seeds = load_seeds(DATASET)
    rng = random.Random(SEED)
    merchants = expand_merchants(merchant_seeds, rng)
    customers = expand_customers(customer_seeds, merchants, rng)
    triggers = expand_triggers(trigger_seeds, merchants, customers, rng)
    store = client.app.state.store

    for scope, records, key in (
        ("category", categories.values(), "slug"),
        ("merchant", merchants, "merchant_id"),
        ("customer", customers, "customer_id"),
        ("trigger", triggers, "id"),
    ):
        for payload in records:
            accepted, _ = store.put_context(
                scope=scope,
                context_id=payload[key],
                version=1,
                payload=payload,
                delivered_at=utc_now(),
            )
            assert accepted

    candidates = 0
    failures = {}
    malformed = {}
    for trigger in triggers:
        record = store.get_context("trigger", trigger["id"])
        candidate = client.app.state.policy.build_candidate(
            record, "2026-08-23T10:00:00Z"
        )
        if candidate is None:
            continue
        candidates += 1
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
            failures[trigger["id"]] = errors
        if any(fragment in plan.fallback_body for fragment in ("None", "is .", ": .", "for .")):
            malformed[trigger["id"]] = plan.fallback_body

    assert candidates >= 50
    assert failures == {}
    assert malformed == {}
