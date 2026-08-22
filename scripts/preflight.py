from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib import error, request


def call(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 15,
) -> tuple[int, dict[str, Any], float]:
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=encoded,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw), time.perf_counter() - started
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, json.loads(raw) if raw else {}, time.perf_counter() - started


def require(label: str, status: int, body: dict[str, Any], predicate: bool, elapsed: float) -> None:
    marker = "PASS" if predicate else "FAIL"
    print(f"[{marker}] {label}: HTTP {status}, {elapsed * 1000:.0f} ms")
    if not predicate:
        raise RuntimeError(f"{label} failed: {body}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Non-destructive public endpoint preflight")
    parser.add_argument("base_url")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    status, body, elapsed = call(base, "GET", "/v1/healthz", timeout=5)
    require("healthz", status, body, status == 200 and body.get("status") == "ok", elapsed)
    status, body, elapsed = call(base, "GET", "/v1/metadata", timeout=5)
    require("metadata", status, body, status == 200 and bool(body.get("team_name")), elapsed)

    suffix = uuid.uuid4().hex[:12]
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    merchant_id = f"preflight_merchant_{suffix}"
    trigger_id = f"preflight_trigger_{suffix}"
    contexts = (
        ("category", "preflight", {"slug": "preflight", "voice": {"tone": "practical", "vocab_taboo": []}, "digest": []}),
        ("merchant", merchant_id, {"merchant_id": merchant_id, "category_slug": "preflight", "identity": {"name": "Preflight Business", "owner_first_name": "Tester"}, "offers": []}),
        ("trigger", trigger_id, {"id": trigger_id, "scope": "merchant", "kind": "curious_ask_due", "merchant_id": merchant_id, "customer_id": None, "payload": {"ask_template": "preflight_service_check"}, "urgency": 1, "suppression_key": f"preflight:{suffix}"}),
    )
    for scope, context_id, payload in contexts:
        status, body, elapsed = call(
            base,
            "POST",
            "/v1/context",
            {"scope": scope, "context_id": context_id, "version": 1, "payload": payload, "delivered_at": now},
            timeout=10,
        )
        require(f"context {scope}", status, body, status == 200 and body.get("accepted") is True, elapsed)

    status, body, elapsed = call(
        base,
        "POST",
        "/v1/tick",
        {"now": now, "available_triggers": [trigger_id]},
    )
    actions = body.get("actions", [])
    require("tick", status, body, status == 200 and len(actions) == 1, elapsed)

    reply_payload = {
        "conversation_id": actions[0]["conversation_id"],
        "merchant_id": merchant_id,
        "customer_id": None,
        "from_role": "merchant",
        "message": "Ok lets do it. Whats next?",
        "received_at": now,
        "turn_number": 2,
    }
    status, body, elapsed = call(base, "POST", "/v1/reply", reply_payload)
    require("reply intent", status, body, status == 200 and body.get("action") == "send", elapsed)
    print("Preflight complete: all five endpoints and a full context -> tick -> reply flow passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
