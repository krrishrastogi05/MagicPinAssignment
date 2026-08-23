from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator
from urllib import error, parse, request


class LiveTestFailure(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@contextmanager
def fixed_dns(hostname: str, ip_address: str | None) -> Iterator[None]:
    """Optionally pin one hostname to an IP while preserving HTTPS SNI."""

    if not ip_address:
        yield
        return

    original = socket.getaddrinfo

    def resolve(
        host: str,
        port: int | str | None,
        family: int = 0,
        type_: int = 0,
        proto: int = 0,
        flags: int = 0,
    ):
        target = ip_address if str(host).lower() == hostname.lower() else host
        return original(target, port, family, type_, proto, flags)

    socket.getaddrinfo = resolve  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.getaddrinfo = original  # type: ignore[assignment]


class Client:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        # Ignore machine-wide proxies so the test reflects the public service.
        self.opener = request.build_opener(request.ProxyHandler({}))

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 30,
        retries: int = 1,
    ) -> tuple[int, dict[str, Any], dict[str, str], float]:
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        last_error: BaseException | None = None
        for attempt in range(retries):
            # Measure the server-facing attempt, not time spent recovering from
            # a failed local route before a retry.
            started = time.perf_counter()
            req = request.Request(
                f"{self.base_url}{path}",
                data=encoded,
                method=method,
                headers={"Content-Type": "application/json"},
            )
            try:
                with self.opener.open(req, timeout=timeout) as response:
                    raw = response.read().decode("utf-8")
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    return (
                        response.status,
                        json.loads(raw) if raw else {},
                        headers,
                        time.perf_counter() - started,
                    )
            except error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                headers = {key.lower(): value for key, value in exc.headers.items()}
                return (
                    exc.code,
                    json.loads(raw) if raw else {},
                    headers,
                    time.perf_counter() - started,
                )
            except (error.URLError, TimeoutError, socket.timeout) as exc:
                last_error = exc
                if attempt + 1 < retries:
                    time.sleep(0.5 * (attempt + 1))

        raise LiveTestFailure(f"{method} {path} could not connect: {last_error}")


class Reporter:
    def __init__(self):
        self.passed = 0

    def check(self, label: str, condition: bool, details: str = "") -> None:
        if not condition:
            suffix = f" — {details}" if details else ""
            print(f"[FAIL] {label}{suffix}")
            raise LiveTestFailure(f"{label}{suffix}")
        self.passed += 1
        suffix = f" — {details}" if details else ""
        print(f"[PASS] {label}{suffix}")


def accepted_context(
    reporter: Reporter,
    client: Client,
    *,
    scope: str,
    context_id: str,
    payload: dict[str, Any],
    now: str,
) -> None:
    status, body, _, elapsed = client.call(
        "POST",
        "/v1/context",
        {
            "scope": scope,
            "context_id": context_id,
            "version": 1,
            "payload": payload,
            "delivered_at": now,
        },
        timeout=10,
    )
    reporter.check(
        f"context/{scope}",
        status == 200 and body.get("accepted") is True and elapsed < 5,
        f"HTTP {status}, {elapsed:.2f}s",
    )


def run(base_url: str) -> int:
    client = Client(base_url)
    reporter = Reporter()
    suffix = uuid.uuid4().hex[:12]
    prefix = f"__vera_probe__{suffix}"
    category_id = f"{prefix}_category"
    merchant_id = f"{prefix}_merchant"
    digest_id = f"{prefix}_digest"
    trigger_id = f"{prefix}_trigger"
    now = utc_now()

    status, health, _, elapsed = client.call(
        "GET", "/v1/healthz", timeout=5, retries=3
    )
    reporter.check(
        "health contract",
        status == 200
        and health.get("status") == "ok"
        and set(health.get("contexts_loaded", {}))
        == {"category", "merchant", "customer", "trigger"}
        and elapsed < 5,
        f"HTTP {status}, {elapsed:.2f}s",
    )
    initial_counts = dict(health["contexts_loaded"])

    status, metadata, _, elapsed = client.call(
        "GET", "/v1/metadata", timeout=5, retries=3
    )
    reporter.check(
        "metadata identity",
        status == 200
        and metadata.get("team_name") == "RisingStar"
        and metadata.get("team_members") == ["Krrish Rastogi"]
        and elapsed < 5,
        f"HTTP {status}, {elapsed:.2f}s",
    )
    configured_model = str(metadata.get("model", ""))
    reporter.check(
        "Gemini configured",
        "google:gemini-3.7-flash" in configured_model,
        configured_model or "missing model label",
    )

    status, invalid, _, _ = client.call(
        "POST",
        "/v1/context",
        {
            "scope": "merchant",
            "context_id": f"{prefix}_invalid",
            "version": 1,
            "payload": {},
            "delivered_at": "not-a-timestamp",
        },
        timeout=10,
    )
    reporter.check(
        "invalid context rejected",
        status == 422 and bool(invalid.get("detail")),
        f"HTTP {status}",
    )

    accepted_context(
        reporter,
        client,
        scope="category",
        context_id=category_id,
        payload={
            "slug": category_id,
            "voice": {"tone": "practical and locally relevant", "vocab_taboo": ["guaranteed"]},
            "peer_stats": {"avg_ctr": 0.035},
            "digest": [
                {
                    "id": digest_id,
                    "title": "Local searches for express services rose 18% this week",
                    "source": "RisingStar live-test fixture",
                    "summary": "Use a concrete active offer and one low-friction next step.",
                }
            ],
        },
        now=now,
    )
    accepted_context(
        reporter,
        client,
        scope="merchant",
        context_id=merchant_id,
        payload={
            "merchant_id": merchant_id,
            "category_slug": category_id,
            "identity": {
                "name": "RisingStar Test Cafe",
                "owner_first_name": "Krrish",
                "locality": "Gurugram",
            },
            "performance": {"ctr": 0.021},
            "offers": [
                {"title": "Lunch Combo at ₹299", "status": "active"}
            ],
        },
        now=now,
    )
    accepted_context(
        reporter,
        client,
        scope="trigger",
        context_id=trigger_id,
        payload={
            "id": trigger_id,
            "scope": "merchant",
            "kind": "research_digest",
            "merchant_id": merchant_id,
            "customer_id": None,
            "payload": {"digest_item_id": digest_id},
            "urgency": 9,
            "suppression_key": f"{prefix}:suppression",
        },
        now=now,
    )

    status, stale, _, _ = client.call(
        "POST",
        "/v1/context",
        {
            "scope": "merchant",
            "context_id": merchant_id,
            "version": 1,
            "payload": {},
            "delivered_at": now,
        },
        timeout=10,
    )
    reporter.check(
        "context idempotency",
        status == 409
        and stale.get("accepted") is False
        and stale.get("reason") == "stale_version"
        and stale.get("current_version") == 1,
        f"HTTP {status}",
    )

    tick_payload = {"now": now, "available_triggers": [trigger_id]}
    status, tick, headers, elapsed = client.call(
        "POST", "/v1/tick", tick_payload, timeout=30
    )
    tick_elapsed = elapsed
    actions = tick.get("actions", [])
    reporter.check(
        "tick contract and latency",
        status == 200 and len(actions) == 1 and elapsed < 30,
        f"HTTP {status}, {elapsed:.2f}s, actions={len(actions)}",
    )
    action = actions[0]
    reporter.check(
        "action contract",
        action.get("merchant_id") == merchant_id
        and action.get("trigger_id") == trigger_id
        and action.get("send_as") == "vera"
        and action.get("suppression_key") == f"{prefix}:suppression"
        and action.get("cta") == "binary_yes_no"
        and bool(action.get("conversation_id")),
    )
    body = str(action.get("body", ""))
    reporter.check(
        "grounded message quality",
        bool(body)
        and body.count("?") <= 1
        and "http" not in body.lower()
        and "guaranteed" not in body.lower()
        and any(
            fact in body
            for fact in ("18%", "2.1%", "3.5%", "₹299", "RisingStar live-test fixture")
        ),
        body,
    )

    composer = headers.get("x-vera-composer", "missing")
    composer_detail = headers.get("x-vera-composer-detail", "missing")
    reporter.check(
        "Gemini actually used",
        "google:gemini" in composer
        and "deterministic-fallback" not in composer
        and "minimal-safe-fallback" not in composer,
        f"X-Vera-Composer={composer}; detail={composer_detail}",
    )

    status, duplicate_tick, duplicate_headers, elapsed = client.call(
        "POST", "/v1/tick", tick_payload, timeout=30
    )
    reporter.check(
        "suppression deduplication",
        status == 200
        and duplicate_tick.get("actions") == []
        and duplicate_headers.get("x-vera-composer") == "none"
        and elapsed < 30,
        f"HTTP {status}, {elapsed:.2f}s",
    )

    conversation_id = str(action["conversation_id"])
    status, reply, _, elapsed = client.call(
        "POST",
        "/v1/reply",
        {
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": None,
            "from_role": "merchant",
            "message": "Yes, draft it. What is next?",
            "received_at": now,
            "turn_number": 2,
        },
        timeout=30,
    )
    reporter.check(
        "reply commitment transition",
        status == 200
        and reply.get("action") == "send"
        and bool(reply.get("body"))
        and elapsed < 30,
        f"HTTP {status}, {elapsed:.2f}s",
    )

    status, stopped, _, elapsed = client.call(
        "POST",
        "/v1/reply",
        {
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": None,
            "from_role": "merchant",
            "message": "STOP",
            "received_at": now,
            "turn_number": 3,
        },
        timeout=30,
    )
    reporter.check(
        "reply consent safety",
        status == 200 and stopped.get("action") == "end" and elapsed < 30,
        f"HTTP {status}, {elapsed:.2f}s",
    )

    def health_probe(_: int) -> tuple[int, float]:
        probe_status, _, _, probe_elapsed = client.call(
            "GET", "/v1/healthz", timeout=5, retries=2
        )
        return probe_status, probe_elapsed

    with ThreadPoolExecutor(max_workers=10) as pool:
        burst = list(pool.map(health_probe, range(10)))
    reporter.check(
        "10-request health burst",
        all(status == 200 and elapsed < 5 for status, elapsed in burst),
        ", ".join(f"{status}/{elapsed:.2f}s" for status, elapsed in burst),
    )

    status, final_health, _, elapsed = client.call(
        "GET", "/v1/healthz", timeout=5, retries=3
    )
    counts = final_health.get("contexts_loaded", {})
    reporter.check(
        "probe isolation from evaluator counts",
        status == 200
        and elapsed < 5
        and counts == initial_counts,
        f"counts={counts}",
    )

    print(
        f"\nLIVE E2E PASSED: {reporter.passed} checks; Gemini composer={composer}; "
        f"tick={tick_elapsed:.2f}s (contract maximum: 30s)."
    )
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Full public Vera API test, including proof of Gemini composition"
    )
    parser.add_argument("base_url")
    parser.add_argument(
        "--resolve-ip",
        help="Optional fixed IPv4 address for flaky local DNS/routes; HTTPS hostname verification is preserved.",
    )
    args = parser.parse_args()
    parsed = parse.urlsplit(args.base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        parser.error("base_url must be a public https URL")

    try:
        with fixed_dns(parsed.hostname, args.resolve_ip):
            return run(args.base_url)
    except LiveTestFailure as exc:
        print(f"\nLIVE E2E FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
