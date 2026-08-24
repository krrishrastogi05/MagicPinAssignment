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
    judge_simulator.LLM_MODEL = args.model or "gemini-3.6-flash"

    # Gemini 3.x flash is a thinking model: with the harness's 1500-token cap it
    # spends the budget reasoning and returns no JSON, so the judge silently
    # falls back to flat 5s. Disable thinking and widen the cap for a reliable,
    # parseable score. Patches only this run — judge_simulator.py stays pristine.
    import json as _json
    from urllib import request as _rq

    def _complete(self, prompt, system=None):
        full = f"{system}\n\n{prompt}" if system else prompt
        body = _json.dumps({
            "contents": [{"parts": [{"text": full}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 4000,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }).encode("utf-8")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        req = _rq.Request(url, data=body, headers={"Content-Type": "application/json"})
        data = _json.loads(_rq.urlopen(req, timeout=judge_simulator.TIMEOUT_LLM).read().decode("utf-8"))
        return data["candidates"][0]["content"]["parts"][0]["text"]

    judge_simulator.GeminiProvider.complete = _complete
    provider = judge_simulator.create_provider()
    ok = judge_simulator.JudgeSimulator(provider).run(args.scenario)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
