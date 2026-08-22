# RisingStar — Vera Grounded Bot

An evaluation-first bot for the magicpin Vera AI Challenge. It exposes the five required public endpoints and is designed to stay useful when the model is slow, unavailable, or tempted to invent details.

The main design rule is simple: deterministic code decides **whether to send, who receives it, which facts are allowed, and what CTA is valid**. An optional PydanticAI composer may improve the wording, but its structured output must pass the same provenance validator as the built-in copy. Invalid or timed-out generations fall back automatically.

## What is implemented

- `POST /v1/context` — atomic, monotonic versioned storage with a 500 KB limit.
- `POST /v1/tick` — deterministic prioritization, consent checks, cooldowns, one action per recipient, a 20-action cap, and race-safe suppression.
- `POST /v1/reply` — commitment, STOP/hostility, pause, repeated auto-reply, question, off-topic, and ambiguity handling.
- `GET /v1/healthz` — model-independent health and context counts.
- `GET /v1/metadata` — team and implementation identity.
- Durable SQLite memory for contexts, conversations, turns, suppression keys, model-generation cache, cooldowns, and merchant-level auto-reply fingerprints.
- Optional PydanticAI structured generation with provider/model fallback.
- Docker packaging, dataset loader, synthetic public preflight, and a wrapper around the bundled official simulator.

The full strategy and evaluator analysis are in [plan.md](plan.md).

## Run locally

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

On macOS/Linux, replace `.\.venv\Scripts\python.exe` with `.venv/bin/python`.

Check the live flow from another terminal:

```powershell
.\.venv\Scripts\python.exe scripts\preflight.py http://localhost:8080
.\.venv\Scripts\python.exe scripts\load_test.py http://localhost:8080
.\.venv\Scripts\python.exe scripts\run_official_simulator.py --scenario phase2_short
.\.venv\Scripts\python.exe scripts\run_official_simulator.py --scenario all
```

The simulator wrapper intentionally uses the bundled judge's basic offline scoring fallback. To obtain a real LLM judge score, configure a provider in `judge_simulator.py` or supply the evaluator key during the final tuning pass.

## Run the tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The suite covers:

- contract and schema validation;
- stale and concurrent context versions;
- concurrent tick deduplication;
- consent and customer send-as behavior;
- every seed message's numeric and fact provenance;
- all 100 expanded triggers, including sparse placeholders;
- intent transitions, auto-reply loops across changed conversation IDs, STOP, wait, off-topic, and ambiguous replies;
- real PydanticAI initialization.

## Gemini setup

Gemini is enabled by default, while the deterministic composer remains the automatic outage and validation fallback. Copy `.env.example` to `.env`, create a key in Google AI Studio, and replace only the key placeholder:

```dotenv
VERA_MODEL_ENABLED=true
VERA_MODELS=google:gemini-3.7-flash,google:gemini-3.6-flash
GOOGLE_API_KEY=your-secret-key
```

Provider-qualified model IDs are tried in order. Generation is bounded by `VERA_MODEL_TIMEOUT_SECONDS` (six seconds by default). The LLM never chooses triggers and cannot bypass the fact/number, URL, taboo, CTA, repetition, or output-schema checks.

Do not commit `.env` or paste the key into source code. Leaving `VERA_MODEL_ENABLED=false` still gives a fully working deterministic submission.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `VERA_DATABASE_PATH` | `data/vera.db` | Durable SQLite path |
| `VERA_MODEL_ENABLED` | `true` | Enable validated Gemini composition |
| `VERA_MODELS` | Gemini 3.7 Flash, then 3.6 Flash | Comma-separated provider-qualified model IDs |
| `VERA_MODEL_TIMEOUT_SECONDS` | `6` | Hard composition timeout |
| `VERA_EVALUATION_NO_URLS` | `true` | Reject URLs in generated copy |
| `VERA_STRICT_EXPIRY` | `false` | Enforce trigger timestamps rather than treating `available_triggers` as authoritative |
| `VERA_MAX_ACTIONS_PER_TICK` | `20` | Output cap, always clamped to 20 |
| `VERA_TEAM_NAME` | `RisingStar` | Required submission metadata |
| `VERA_TEAM_MEMBERS` | `Krrish Rastogi` | Comma-separated names |
| `VERA_CONTACT_EMAIL` | `krrishrastogi00@gmail.com` | Submission contact |
| `VERA_SUBMITTED_AT` | `not-submitted` | ISO submission timestamp |

`VERA_STRICT_EXPIRY` defaults to false because the bundled simulator sends its current wall-clock time while the provided seed triggers use 2026 challenge dates. The evaluator's `available_triggers` list is therefore treated as authoritative unless strict mode is explicitly enabled.

## Load challenge data

The bot does not require preloaded contexts; the evaluator pushes them. For manual testing:

```powershell
.\.venv\Scripts\python.exe scripts\load_dataset.py http://localhost:8080
```

The loader accepts either the bundled seed layout or the generated expanded layout.

## Docker and Railway deployment

```powershell
docker compose up --build
```

The image runs one Uvicorn worker deliberately: SQLite handles concurrent requests, while one process keeps the database and in-memory model setup simple. The named volume persists deduplication and conversation memory across container restarts.

Railway is the shortest deployment path for this repository:

1. Push this folder to a private or public GitHub repository.
2. In Railway, create a project from that repository. The root `Dockerfile` and `railway.json` are detected automatically.
3. Add a persistent volume mounted at `/data`.
4. Add these service variables: `GOOGLE_API_KEY`, `VERA_DATABASE_PATH=/data/vera.db`, `VERA_MODEL_ENABLED=true`, `VERA_MODELS=google:gemini-3.7-flash,google:gemini-3.6-flash`, and `RAILWAY_RUN_UID=0`.
5. Generate a public domain and keep exactly one replica running throughout evaluation.

`RAILWAY_RUN_UID=0` is required because Railway volumes are mounted as root while this image normally runs as the non-root `vera` user. The secret belongs only in Railway's Variables screen. After deployment, run:

```powershell
.\.venv\Scripts\python.exe scripts\preflight.py https://YOUR-PUBLIC-BASE-URL
```

The preflight uses unique synthetic context and suppression IDs, so it does not consume any official challenge trigger.

## Final submission checklist

The team name, participant name, email, and Gemini model choice are configured. The phone number is intentionally not stored or exposed by the bot. Remaining external steps are:

- optional LinkedIn URL;
- a Google AI Studio API key stored as a Railway secret;
- Railway/GitHub sign-in and the generated public base URL;
- `VERA_SUBMITTED_AT` set to the final UTC submission time;
- the phone number entered directly in the magicpin form.
