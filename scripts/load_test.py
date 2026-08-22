from __future__ import annotations

import argparse
import asyncio
import math
import time

import httpx


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percent * len(ordered)) - 1))
    return ordered[index]


async def main_async(base_url: str, requests: int, concurrency: int) -> int:
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    failures: list[str] = []

    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=5) as client:
        async def one(index: int) -> None:
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.get("/v1/healthz")
                    elapsed = time.perf_counter() - started
                    latencies.append(elapsed)
                    if response.status_code != 200 or response.json().get("status") != "ok":
                        failures.append(f"{index}: HTTP {response.status_code}")
                except Exception as exc:
                    failures.append(f"{index}: {exc}")

        started = time.perf_counter()
        await asyncio.gather(*(one(index) for index in range(requests)))
        wall = time.perf_counter() - started

    if not latencies:
        print(f"No successful requests; failures={len(failures)}")
        return 1
    print(
        f"requests={requests} concurrency={concurrency} failures={len(failures)} "
        f"throughput={requests / wall:.1f} rps p50={percentile(latencies, .50) * 1000:.0f} ms "
        f"p95={percentile(latencies, .95) * 1000:.0f} ms p99={percentile(latencies, .99) * 1000:.0f} ms"
    )
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Concurrent health endpoint load check")
    parser.add_argument("base_url")
    parser.add_argument("--requests", type=int, default=250)
    parser.add_argument("--concurrency", type=int, default=25)
    args = parser.parse_args()
    return asyncio.run(main_async(args.base_url, args.requests, args.concurrency))


if __name__ == "__main__":
    raise SystemExit(main())
