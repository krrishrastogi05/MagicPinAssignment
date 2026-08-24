"""Run the official judge_simulator with the real Gemini LLM judge.

Reads the key from the GEMINI_API_KEY env var so it never lands on disk or in
git. Usage:
    GEMINI_API_KEY=... python scripts/run_real_judge.py --url https://... --scenario full_evaluation
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import judge_simulator

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://web-production-5474c.up.railway.app")
    parser.add_argument("--scenario", default="full_evaluation")
    parser.add_argument("--model", default=os.getenv("GEMINI_JUDGE_MODEL", ""))
    args = parser.parse_args()

    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        print("GEMINI_API_KEY env var is not set", file=sys.stderr)
        return 2

    judge_simulator.BOT_URL = args.url.rstrip("/")
    judge_simulator.LLM_PROVIDER = "gemini"
    judge_simulator.LLM_API_KEY = key
    judge_simulator.LLM_MODEL = args.model
    provider = judge_simulator.create_provider()
    ok = judge_simulator.JudgeSimulator(provider).run(args.scenario)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
