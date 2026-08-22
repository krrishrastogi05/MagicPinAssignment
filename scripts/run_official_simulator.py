from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import judge_simulator

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class OfflineScoringProvider(judge_simulator.LLMProvider):
    """For contract testing only; composition scoring falls back to the judge heuristic."""

    def name(self) -> str:
        return "offline-contract-check"

    def complete(self, prompt: str, system: str | None = None) -> str:
        raise RuntimeError("offline scoring requested")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the bundled official harness without an evaluator API key")
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument(
        "--scenario",
        default="all",
        choices=("warmup", "phase2_short", "auto_reply_hell", "intent_transition", "hostile", "all", "full_evaluation"),
    )
    args = parser.parse_args()
    judge_simulator.BOT_URL = args.url.rstrip("/")
    success = judge_simulator.JudgeSimulator(OfflineScoringProvider()).run(args.scenario)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
