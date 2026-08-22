from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib import error, request


ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def records(dataset: Path, scope: str) -> Iterable[tuple[str, dict[str, Any]]]:
    if scope == "category":
        for path in sorted((dataset / "categories").glob("*.json")):
            payload = _read(path)
            yield str(payload["slug"]), payload
        return

    plural, key = {
        "merchant": ("merchants", "merchant_id"),
        "customer": ("customers", "customer_id"),
        "trigger": ("triggers", "id"),
    }[scope]
    seed = dataset / f"{plural}_seed.json"
    if seed.exists():
        for payload in _read(seed)[plural]:
            yield str(payload[key]), payload
        return
    for path in sorted((dataset / plural).glob("*.json")):
        payload = _read(path)
        yield str(payload[key]), payload


def post_json(url: str, payload: dict[str, Any], timeout: float) -> tuple[int, Any]:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=encoded,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, json.loads(raw) if raw else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Load seed or expanded Vera contexts")
    parser.add_argument("base_url", help="Bot base URL, e.g. http://localhost:8080")
    parser.add_argument("--dataset", type=Path, default=ROOT / "dataset")
    parser.add_argument("--version", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--skip-customers", action="store_true")
    args = parser.parse_args()

    delivered_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    scopes = ["category", "merchant", "customer", "trigger"]
    if args.skip_customers:
        scopes.remove("customer")
    accepted = conflicts = failed = 0
    for scope in scopes:
        scope_count = 0
        for context_id, payload in records(args.dataset, scope):
            status, body = post_json(
                f"{args.base_url.rstrip('/')}/v1/context",
                {
                    "scope": scope,
                    "context_id": context_id,
                    "version": args.version,
                    "payload": payload,
                    "delivered_at": delivered_at,
                },
                args.timeout,
            )
            scope_count += 1
            if status == 200 and body.get("accepted") is True:
                accepted += 1
            elif status == 409 and body.get("reason") == "stale_version":
                conflicts += 1
            else:
                failed += 1
                print(f"FAIL {scope}/{context_id}: HTTP {status} {body}")
        print(f"{scope}: {scope_count}")
    print(f"accepted={accepted} existing_or_stale={conflicts} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
