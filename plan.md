# Vera by magicpin — Evaluation-First Build and Submission Plan

**Status:** implemented and locally verified; RisingStar identity and Gemini are configured; awaiting secret and public Railway deployment  
**Prepared:** 2026-08-22; implementation checkpoint 2026-08-23  
**Goal:** ship a public, reliable bot that makes grounded decisions, produces merchant-worthy WhatsApp messages, handles replay conversations cleanly, and remains live throughout evaluation.

### Implementation checkpoint

The strategy below has now been implemented as a FastAPI service in `app/`, with exact SQLite memory, deterministic decision and suppression logic, a provenance-constrained Gemini composer, and a deterministic reply state machine. Deployment and operator tooling are provided through `Dockerfile`, `compose.yaml`, `railway.json`, `scripts/preflight.py`, `scripts/load_test.py`, `scripts/load_dataset.py`, and `scripts/run_official_simulator.py`.

Verified on 2026-08-23:

- 20 automated tests pass, including concurrent version races, simultaneous tick deduplication, and concurrent model composition across independent recipients.
- Every viable seed plan passes fact/number/CTA/taboo validation.
- All 100 expanded triggers either abstain safely or produce non-malformed grounded plans.
- The five-endpoint live HTTP preflight passes.
- The bundled simulator passes phase 2, auto-reply, intent-transition, and hostile-message scenarios.
- The bundled full evaluation completes all five trigger batches without endpoint failures.
- A 250-request, 25-concurrency health test completes with zero failures at about 168 requests/second, 310 ms p95, and 451 ms p99 on the local Windows host.
- The Linux Docker image builds and its black-box preflight passes from a running container.

Remaining work requires external access rather than more core implementation: a Gemini API key, Railway/GitHub sign-in, public HTTPS deployment, public preflight, real LLM-judge tuning, and form submission. The configured identity is RisingStar / Krrish Rastogi / krrishrastogi00@gmail.com; the phone remains form-only and is never exposed by the bot.

## 1. Executive strategy

Build a **Grounded Policy Engine**, not a prompt wrapper.

The bot should use deterministic code to decide whether to send, which trigger wins, which facts are allowed, what action the merchant can take, and how a reply changes conversation state. A language model may realize the final wording, but it must operate inside a narrow fact and output contract. Every generated message passes a grounding and policy validator; a deterministic category-aware template is always available as a fallback.

This architecture directly targets what the evaluator rewards:

- **Decision quality:** choose one timely, actionable signal instead of echoing every available fact.
- **Specificity:** use only traceable values from the latest context version.
- **Category fit:** apply the supplied voice, vocabulary, taboo, and offer rules.
- **Merchant fit:** use this merchant's current name, location, metrics, offer, history, and language.
- **Engagement compulsion:** externalize the work and ask for one low-effort decision.
- **Operational quality:** deterministic output, atomic context updates, suppression, low latency, and graceful failure.

The intended product signature is:

> **One reason now + one merchant-specific proof + one easy next action.**

Do not optimize for the 30 visible pairs. Optimize for the policy that generates good decisions on unseen context.

## 2. Verified challenge picture

### Live submission status

As checked on 2026-08-22, the [official magicpin challenge page](https://partners.magicpin.in/vera/ai-challenge) says submissions are open and shows no fixed closing date (`OPEN NOW`, `Submit anytime`). The entry is solo, the submitted artifact is one public base URL, and the bot must remain live for evaluation.

Required public endpoints:

- `POST /v1/context`
- `POST /v1/tick`
- `POST /v1/reply`
- `GET /v1/healthz`
- `GET /v1/metadata`

Required submission fields:

- Full name
- Valid email
- Valid phone number
- Public bot base URL
- LinkedIn URL, optional

The official page is the current source of truth if any bundled document differs.

### Evaluator lifecycle

1. Health and metadata check.
2. Base category, merchant, and customer contexts are loaded.
3. During a 60-simulated-minute window, new context is pushed and `/v1/tick` is called every five simulated minutes.
4. The evaluator injects fresh digests, changed metrics, new triggers, and surprise customer scopes.
5. High-scoring bots receive replay tests for auto-replies, explicit intent transitions, hostile replies, and off-topic messages.
6. Three consecutive health failures disqualify a run.

Published operational limits:

| Constraint | Required | Internal target |
|---|---:|---:|
| Judge traffic | 10 requests/second | Sustain 25 requests/second locally |
| Context payload | 500 KB maximum | Accept 500 KB in under 500 ms |
| Actions per tick | 20 maximum | Deterministically return 0–20 |
| Overall timeout | 30 seconds | Tick/reply p95 under 8 seconds |
| Local simulator timeout | 15 seconds for tick/reply | Never rely on the 30-second ceiling |
| Health checks | Three failures disqualify | p99 under 250 ms; no model dependency |

### Repository checks completed

- The repository contains the complete FastAPI bot, durable policy memory, Gemini integration, deployment packaging, and evaluator tooling described in this plan.
- The dataset generator ran successfully in a temporary directory and produced 5 categories, 50 merchants, 200 customers, 100 triggers, and a 30-pair `test_pairs.json`.
- The generator is deterministic, but many generated triggers deliberately contain sparse placeholder payloads. The bot must tolerate incomplete and novel contexts without fabricating details.
- `judge_simulator.py` loads the seed files directly: 5 categories, 10 merchants, 15 customers, and 25 triggers. It does not exercise the full expanded dataset.
- The local warmup pushes only five merchants; the local full run does not exercise customer messages; its endpoint validation is shallow. It is an anchor, not proof of readiness.
- The local simulator uses stricter 5/10/15-second client timeouts even though the public limit is 30 seconds.
- The simulator's auto-reply routine changes `conversation_id` across repetitions, so auto-reply fingerprints must also be tracked at merchant level.
- The simulator judge itself uses sampling (`temperature: 0.2` in several providers). Measure a distribution of judge scores, not a single lucky run.

### Specification conflicts and chosen behavior

| Conflict | Decision for evaluation mode |
|---|---|
| Old `challenge-brief.md` mentions `bot.py` + JSONL; the live page and testing brief require a public URL | Build and submit the five-endpoint HTTP service. Treat the old artifact section as superseded. |
| Same context version is described as a no-op, while the detailed API example expects `409` | Make it a true no-op and return `409` with `accepted: false`, `reason: stale_version`, and `current_version`. Higher versions replace atomically. |
| One document allows useful links, while the API failure examples mark URLs as a hard failure | Generate no URLs in evaluation mode. Keep link emission behind a disabled feature flag. |
| The website says one clear CTA, while booking examples use two slot choices | One decision can contain bounded choices. Use one question/decision point, never two unrelated asks. |
| The public limit is 30 seconds, but the supplied client uses 15 seconds | Budget 8 seconds p95 and 12 seconds absolute before fallback. |

## 3. Definition of a winning submission

The bot is ready only when all of these are true:

- Required endpoint contract tests pass 100%.
- Identical effective inputs produce byte-identical response bodies across 20 repetitions and after a process restart.
- Context version updates are atomic under concurrent writes.
- No action contains a number, date, price, percentage, source, place, service, or named entity that cannot be traced to an input JSON pointer or an approved formatting transformation.
- A tick never sends more than one proactive message to the same recipient and never exceeds 20 actions.
- Expired, suppressed, opted-out, missing-consent, or context-incomplete triggers do not send.
- Fresh context versions are used immediately; cached output from older snapshots is never reused.
- All visible trigger families and all five categories have high-quality golden tests written in original language.
- Auto-reply, explicit acceptance, wait, hard no, hostility, and off-topic replay tests pass.
- The official judge simulator passes every scenario and achieves a median of at least **44/50** across three scoring runs, with no scored action below **36/50**.
- A 10 requests/second soak test produces zero malformed responses, timeouts, or duplicate sends.
- Model outage, timeout, and invalid model JSON still produce a valid deterministic fallback response.
- The deployed service is always-on, HTTPS reachable, externally probed, and not dependent on a sleeping free tier.

These are engineering targets, not claims about a guaranteed rank.

## 4. Proposed architecture

```text
Judge HTTP request
        |
        v
Envelope validation + request limits
        |
        +--> Versioned context store (SQLite WAL on persistent disk)
        |
        +--> Tick policy: eligibility -> ranking -> recipient dedup -> reservation
        |                                   |
        |                                   v
        |                         fact ledger + message brief
        |                                   |
        |                         constrained model composer
        |                                   |
        |                         grounding/policy validator
        |                                   |
        |                         deterministic fallback
        |
        +--> Reply policy: intent state machine -> grounded response composer
        |
        v
Deterministic JSON response + structured audit event
```

### Recommended stack

- Python 3.12+
- FastAPI with Pydantic request/response models
- One async application worker for evaluation simplicity and consistent state
- SQLite in WAL mode on a persistent volume
- SQL transactions for version checks, suppression reservations, and conversation transitions
- `httpx` or the selected model SDK with strict connect/read deadlines
- `orjson` or canonical sorted JSON for hashing and response serialization
- Pytest, Hypothesis, and an HTTP contract test client
- Docker image with a non-root user and a simple start command

At the published scale, a single process plus SQLite WAL is easier to make correct than a distributed system. If the chosen host cannot provide persistent local disk, replace the repository implementation with Postgres; do not mix in-memory state across multiple workers.

### Proposed project layout

```text
app/
  main.py                   # FastAPI app and lifecycle
  config.py                 # environment configuration
  api/
    models.py               # tolerant request, strict response schemas
    routes.py               # the five required endpoints
  storage/
    db.py                   # migrations, WAL setup, transactions
    repositories.py         # context, suppression, conversation, generation stores
  policy/
    eligibility.py          # expiry, consent, opt-out, cooldown, context gates
    ranking.py              # deterministic trigger scoring and tie-breaking
    facts.py                # normalization, derivation, provenance ledger
    category.py             # category voice and taboo enforcement
  compose/
    briefs.py               # trigger-family message plans
    model.py                # structured model call with deadlines
    validate.py             # grounding, CTA, taboo, URL, repetition checks
    fallback.py             # deterministic category-aware renderer
  reply/
    classify.py             # deterministic intent and auto-reply classification
    state_machine.py        # send/wait/end transitions
    handlers.py             # per-intent grounded handlers
  observability.py          # redacted logs and metrics
tests/
  contract/
  unit/
  property/
  golden/
  replay/
  load/
scripts/
  load_dataset.py
  smoke.py
  public_preflight.py
Dockerfile
compose.yaml
pyproject.toml
.env.example
README.md                   # one page, evaluator-focused
```

## 5. Persistence and data model

Use these logical tables:

### `contexts`

Key: `(scope, context_id)`

Fields: `version`, canonical `payload_json`, `payload_hash`, `delivered_at`, `stored_at`.

Rules:

- Accept only scopes `category`, `merchant`, `customer`, and `trigger`.
- Require a non-empty `context_id`, non-negative integer version, object payload, and parseable timestamp.
- Allow unknown nested fields so fresh evaluator context is not rejected.
- If incoming `version <= current_version`, do not mutate and return the documented stale response.
- If incoming version is higher, replace payload and version in one transaction.
- The context envelope's `context_id` is authoritative for storage; record but do not blindly trust duplicate IDs inside payload.
- `healthz.contexts_loaded` counts the latest row per scope, not writes or versions.

### `suppressions`

Key: `(suppression_key, recipient_id)`

Fields: `trigger_id`, `conversation_id`, `status`, `reserved_at`, `sent_at`, `expires_at`.

Reserve before composition inside a transaction. This prevents concurrent ticks from producing duplicates. A successful fallback still completes the reservation; an unrecoverable internal error releases it.

### `conversations` and `turns`

Store the initiating trigger, merchant, optional customer, latest context snapshot hashes, state, last action, intent, auto-reply count, and turn history. Use the judge's `turn_number` for validation but do not assume calls arrive perfectly in order.

### `merchant_reply_fingerprints`

Track normalized message hashes by merchant as well as by conversation. This catches repeated WhatsApp Business auto-replies even when the evaluator changes conversation IDs.

### `generations`

Key by the SHA-256 hash of:

- Latest category, merchant, trigger, and optional customer payload hashes
- Tick/reply input that affects the result
- Policy version
- Prompt version
- Model identifier

Persist the accepted response JSON, selected fact IDs, validator outcome, latency, and whether fallback was used. This cache is the final determinism guarantee.

## 6. Endpoint contract

### `POST /v1/context`

Implementation steps:

1. Enforce JSON and payload limits before parsing deeply.
2. Validate the envelope while allowing unknown payload fields.
3. Canonicalize the payload and compute its hash.
4. Compare and update inside one transaction.
5. Return a stable acknowledgement ID derived from scope, context ID, and version.

Test all of: first insert, exact retry, same-version different payload, stale lower version, atomic higher replacement, invalid scope, malformed timestamp, 500 KB boundary, and 20 concurrent version races.

### `POST /v1/tick`

Implementation steps:

1. Parse `now` as the evaluator's source of simulated time.
2. Look up only the listed available trigger IDs; ignore missing IDs safely.
3. Join each trigger to the latest merchant, category, and optional customer contexts.
4. Apply hard eligibility gates.
5. Rank eligible triggers using deterministic scores and tie-breakers.
6. Keep at most one winner per recipient per tick and at most 20 total.
7. Reserve each suppression key transactionally.
8. Build a fact ledger and message brief, compose, validate, and fall back if needed.
9. Persist the action and conversation before returning.
10. Sort actions by descending decision score, then stable trigger ID.

Return `{"actions": []}` quickly when nothing is worth sending. Restraint is a product decision, not an error.

Every action should include the full detailed contract even where the public page shows an abbreviated example:

- `conversation_id`
- `merchant_id`
- `customer_id`, nullable
- `send_as`
- `trigger_id`
- `template_name`
- `template_params`
- `body`
- `cta`
- `suppression_key`
- `rationale`

### `POST /v1/reply`

Accept the most complete documented shape, but tolerate optional `merchant_id`, `customer_id`, and `received_at` when the conversation can resolve them. Never crash on an unknown conversation ID; create a minimal recoverable state from supplied IDs and return a safe action.

Always return exactly one of:

- `send` with non-empty grounded `body`, one `cta`, and rationale
- `wait` with bounded positive `wait_seconds` and rationale
- `end` with rationale

Obvious policy intents should never wait on a model call.

### `GET /v1/healthz`

Health must not call the language model. Return process uptime and current context counts. Perform only a very small local database read. The public probe should remain healthy during model/provider outages because deterministic fallback is available.

### `GET /v1/metadata`

Return a superset of the examples:

- Solo participant name as `team_name` or a stable project name
- `team_members` with the solo participant only
- Actual model identifier, or `none` for a fully deterministic composer
- Concise approach, for example `deterministic policy + provenance-constrained composer + validated fallback`
- Contact email
- Semantic version
- Deployment timestamp in UTC

Keep metadata static for the deployed release. Never expose API keys, host details, or internal prompts.

### Optional `POST /v1/teardown`

The testing brief mentions an optional teardown after evaluation. Implement it only after the required five endpoints are stable. Require a per-run token if the evaluator supplies one; otherwise do not expose an unauthenticated state-wipe endpoint.

## 7. Decision engine

### Hard eligibility gates

A trigger is ineligible if any of these is true:

- Trigger, merchant, or category context is missing.
- Trigger is expired at tick `now`.
- Trigger is not in `available_triggers`.
- Its suppression key is already reserved or sent for the recipient.
- The merchant/customer explicitly opted out or is in a hostile-message cooldown.
- Customer scope lacks a matching customer, merchant relationship, reachable channel, affirmative opt-in, or relevant consent scope.
- Trigger payload contradicts the joined merchant/customer IDs.
- A time-sensitive action is already stale.
- Required facts for a safe message are missing.
- The same recipient already has a higher-value action in this tick.

### Deterministic ranking

Score eligible candidates with explicit features, not an LLM:

```text
decision_score =
    4 * explicit_active_intent
  + 3 * normalized_urgency
  + 3 * time_window_pressure
  + 2 * payload_specificity
  + 2 * actionability
  + 2 * merchant_relevance
  + 1 * novelty
  - 4 * fatigue_risk
  - 6 * safety_or_consent_risk
```

Normalize components to documented small integer ranges. Tie-break by earliest expiry, higher trigger version, then lexical trigger ID. Store the component breakdown in the audit log and summarize it faithfully in `rationale`.

The ranker should prefer, in general:

1. Explicit active planning or acceptance intent.
2. Safety, compliance, supply, or appointment deadlines.
3. Customer recall/refill/booking actions with consent and real slots.
4. Actionable performance changes tied to a current offer or concrete fix.
5. Research/trends with a credible source and merchant-relevant cohort.
6. Timely events with demonstrated category and merchant fit.
7. Low-urgency curiosity, dormancy, or generic seasonal triggers.

Urgency alone must never rescue an irrelevant or ungrounded trigger. For example, a Diwali trigger 188 days away should normally be skipped.

### Trigger-family playbooks

| Family | Primary evidence | Good next action | Safety rule |
|---|---|---|---|
| Active planning/acceptance | Merchant's latest explicit intent | Produce the draft/plan or ask for final confirmation | Never ask another qualifying question after commitment |
| Compliance/supply | Source, deadline, batch, regulation | Offer a checklist, affected-stock check, or draft notice | Quote exact context; no medical/legal invention |
| Recall/refill/appointment | Due date, last service/refill, real slots | Pick/confirm a slot or delivery | Require consent and correct `send_as` |
| Performance dip/spike | Current metric, baseline/delta, active offer | Draft one targeted recovery/amplification action | Do not imply causation unless supplied |
| Review theme/milestone | Occurrence count, quote/theme, threshold | Draft response/process fix or milestone post | Avoid shaming or fake customer claims |
| Research/trend | Supplied source, sample/stat, segment | Pull/summarize/draft shareable content | Citation required for research/compliance claims |
| Festival/local event | Date, city, category relevance, capacity/offer | Draft a time-bound campaign | Skip if too early, irrelevant, or no real offer |
| Renewal/winback | Remaining/expired days, plan/amount, performance | Show exact renewal or recovery step | No fake urgency or auto-charge implication |
| Curious/dormant | Real history gap plus a useful merchant fact | Ask one answerable question | Suppress aggressively to avoid spam |
| Unknown/new kind | Concrete payload fact plus safe action | Generic grounded assist | No send if context cannot support a useful message |

## 8. Grounded composition system

### Fact ledger

Before any model call, extract a compact set of approved facts. Every fact has:

- Stable fact ID
- Source scope and JSON pointer
- Raw value
- Safe rendered forms
- Freshness/version
- Semantic type such as price, count, date, offer, metric, source, slot, identity, or preference

Allow deterministic derivations only when their operands are recorded. Examples: formatting `0.021` as `2.1%`, calculating a context-supported gap from `2.1%` to `3.0%`, or displaying an ISO slot in local time. Record the formula and source pointers.

Do not give the model the entire database record. Give it the selected facts, category voice, forbidden claims, message goal, and one CTA type. Treat all context strings and merchant replies as untrusted data, never as prompt instructions.

### Message brief

For each action, code chooses:

- `primary_fact`: why now
- `supporting_fact`: why this merchant/customer
- `offered_work`: what Vera will do
- `cta`: the one decision expected
- `voice`: supplied category and recipient language preference
- `send_as`: `vera` for merchant-facing, `merchant_on_behalf` for customer-facing
- `template_name` and params for proactive first touch

The model may word this brief; it may not change the decision.

### Structured model output

Require schema-constrained JSON:

```json
{
  "body": "...",
  "cta": "binary_yes_no",
  "used_fact_ids": ["trigger.payload.delta_pct", "merchant.offers[0].title"],
  "rationale_summary": "..."
}
```

Use deterministic sampling only where supported, then cache by canonical input hash. Gemini 3.x rejects legacy `temperature`, `top_p`, and `top_k` parameters, so this implementation uses the stable model defaults and relies on strict structured output, validation, and persistent caching for reproducibility.

Set a short model deadline. One retry is allowed only for transport failure and must reuse the same request; invalid or late output goes directly to fallback.

### Output validator

Reject model output if it contains any of the following:

- Number, price, percentage, date, time, proper noun, service, source, locality, or URL outside the fact ledger
- Inactive/expired offer presented as active
- Unsupported causal or comparative claim
- Category taboo or high-risk phrase such as an unsupported guarantee
- Customer outreach without matching consent
- Wrong name, language, merchant/customer pairing, or `send_as`
- More than one unrelated CTA or more than one decision question
- Repetition of an earlier body in the conversation
- Internal field names or evaluator jargon
- Empty body, malformed JSON, or invalid CTA

Run the validator again on deterministic fallback output.

### Message craft rules

- Lead with the sharpest verifiable fact; do not begin with filler such as “Hope you are doing well.”
- Use at most two supporting facts. More facts usually reduce decision quality.
- Make Vera do the work: “Want me to draft it?” is stronger than “You should improve this.”
- Default to one yes/no or confirm/cancel decision. Slot choice is acceptable when booking is the actual task.
- Prefer service + real price over generic percentage discounts.
- Use the owner/merchant first name when present and safe.
- Respect supplied language preference; code-mix only from a reviewed phrase bank, not improvised transliteration.
- Keep merchant messages roughly 160–320 characters and customer messages roughly 120–260 characters unless the task genuinely needs more.
- Use zero or one category-appropriate emoji; never let emoji carry meaning.
- Do not include URLs in evaluation mode.
- Make wording original. Use case studies for structure, never copy their sentences.

### Category voice defaults

Always prefer the current `CategoryContext.voice`. Use these only as fallback:

| Category | Voice | Useful vocabulary | Avoid |
|---|---|---|---|
| Dentists | Clinical peer, calm | Recall, fluoride, caries, consultation | Cure, guaranteed outcome, retail hype |
| Salons | Warm, visual, practical | Look, slot, stylist, occasion, service | Clinical tone, vague discount spam |
| Restaurants | Operator-to-operator, timely | Covers, AOV, prep, delivery, match window | Generic inspiration, too many offers |
| Gyms | Coach-like, motivating, specific | Routine, slot, membership, training focus | Body shaming, medical promises |
| Pharmacies | Precise, trustworthy, utility-first | Refill, batch, stock, delivery, pharmacist | Diagnosis, treatment advice, alarmism |

## 9. Reply state machine

Use deterministic classification first. The precedence order matters:

1. Explicit stop/opt-out/hostility
2. High-confidence auto-reply
3. Explicit acceptance/action intent
4. Wait/snooze request
5. Rejection without opt-out
6. Direct in-scope question
7. Off-topic request
8. Ambiguous response

| Intent | Action | Required behavior |
|---|---|---|
| Stop/abuse/spam complaint | `end` | End immediately and suppress future outreach; no final sales pitch |
| First obvious auto-reply | `wait` | Wait about four hours; record normalized merchant-level fingerprint |
| Repeated auto-reply | `wait`, then `end` | Wait 24 hours after second; end on third or sooner when certainty is high |
| Explicit “yes/send/do it/book/confirm” | `send` | Execute or present the concrete next step; do not qualify again |
| “Later/tomorrow/30 minutes” | `wait` | Parse and clamp to a safe positive duration |
| “No/not interested” | `end` | End politely; apply topic cooldown |
| In-scope question | `send` | Answer only from stored facts, then offer one relevant next step |
| Off-topic request | `send` or `end` | Brief boundary; return to one original next action only if no stop signal |
| Ambiguous | `send` | One short clarification grounded in the current task |

Conversation logic must work even if `/v1/reply` arrives for an unknown conversation, omits optional IDs, repeats a turn, or changes the conversation ID for the same merchant.

## 10. Reliability, privacy, and security

- Run an always-on deployment; disable scale-to-zero and automatic sleeping.
- Use a persistent disk and verify it survives a process restart before submission.
- Put model keys only in deployment secrets. Never place a key in `judge_simulator.py`, source control, logs, or metadata.
- Redact phone fields and message bodies from normal logs. Log IDs, hashes, decisions, fact IDs, latency, and error categories.
- Send only the minimum selected facts to the allowed language-model provider. Do not call unrelated third-party APIs with evaluator payloads.
- Validate JSON content type and cap request body size.
- Treat nested context strings and replies as untrusted input to prevent prompt injection.
- Use strict outbound timeouts, a small connection pool, one retry maximum, and a circuit breaker.
- Keep health independent of the model and external network.
- Shut down cleanly so SQLite transactions finish.
- Back up no evaluator data externally. Purge evaluation state when the run is confirmed complete.

## 11. Testing strategy

### Layer 1 — unit tests

- Context scope and envelope validation
- Version ordering and atomic replacement
- Canonical hashing and cache keys
- ISO time parsing, expiry, and slot rendering
- Consent-scope matching
- Trigger ranking and deterministic tie-breaking
- One-recipient/one-action selection
- Suppression reservations under concurrency
- Fact extraction and derived-value provenance
- Numeric/proper-noun/URL grounding validator
- Category taboo and CTA validator
- Reply intent precedence and wait parsing
- Merchant-level auto-reply fingerprinting

### Layer 2 — HTTP contract tests

For every endpoint, assert status, content type, exact required keys, allowed enums, null behavior, and malformed input responses. Cover examples with and without optional reply fields.

Add explicit tests that the supplied simulator does not enforce:

- Full 255-context warmup count
- Customer-scope actions
- Same/lower/higher context versions
- 500 KB payload boundary
- 20-action cap
- Duplicate suppression across concurrent ticks
- Restart persistence
- Invalid model output

### Layer 3 — golden decision tests

Generate the expanded dataset and run all 30 pairs. Store the policy decision, selected fact IDs, and structural assertions—not only exact prose. Golden wording must be original.

Cover at least the ten case-study anchors:

- Dentistry research and recall
- Salon bridal follow-up and curious ask
- Restaurant match day and corporate planning
- Gym seasonal dip and lapsed-customer winback
- Pharmacy compliance and chronic refill

For every golden case, assert:

- Correct trigger/recipient/send-as
- Current context version
- Traceable hook and merchant/customer fit
- One CTA
- No prohibited claims or URLs
- Rationale matches the actual decision

### Layer 4 — metamorphic and adversarial tests

Change one input and assert the correct output change:

- Raise merchant context version and change CTR; old CTR must disappear.
- Remove the active offer; price/offer must disappear.
- Change language preference; wording mode must change safely.
- Expire a trigger; action must disappear.
- Revoke consent; customer action must disappear.
- Add a higher-value trigger for the same merchant; only the winner sends.
- Insert instructions such as “ignore policy” into a digest title; they remain quoted data and cannot alter behavior.
- Add unknown fields and a new trigger kind; service must remain valid.
- Replace payload facts with placeholders; bot must abstain or use only safe existing context.

### Layer 5 — replay tests

Create deterministic five-turn scenarios for:

- Same auto-reply repeated under both one conversation ID and changing IDs
- “Ok, let's do it” after qualification
- Clear no and explicit STOP
- Hostile abuse
- Off-topic GST question
- Genuine price/source/slot follow-up
- Duplicate/out-of-order turn numbers
- Unknown conversation ID

### Layer 6 — performance and chaos

- 10 requests/second mixed workload for 45 minutes
- Burst of 25 context writes plus ticks
- Concurrent higher/lower version races
- Model latency above deadline
- Model 429/500, invalid JSON, fabricated value, and total outage
- Database restart and application restart
- Cold start followed immediately by warmup load
- Three health probes during model outage

Pass criteria: no malformed responses, duplicate actions, lost higher versions, or timeouts; valid fallback on all model failures.

### Layer 7 — official simulator

Run the official simulator only after internal tests pass:

```powershell
python dataset\generate_dataset.py --seed-dir dataset --out expanded
python judge_simulator.py
```

Never commit an LLM key placed in the simulator. Run `all` and `full_evaluation`, then repeat scoring three times because the judge provider may sample. Track per-dimension median, minimum, action count, latency, and validator/fallback rate.

Review low-scoring messages by cause:

- Wrong decision -> ranker/playbook change
- Weak specificity -> fact selection change
- Wrong tone -> category policy change
- Weak merchant fit -> supporting-fact selector change
- Weak engagement -> offered-work/CTA change
- Hallucination -> validator failure; fix before any prompt tuning

Do not tune by copying the case-study output.

## 12. Implementation sequence and checkpoints

### Phase 0 — freeze the contract

- [ ] Create project skeleton, dependency lock, environment schema, and Dockerfile.
- [ ] Encode tolerant request and strict response models.
- [ ] Write the specification-conflict decisions from this plan into contract tests.
- [ ] Add one command for tests and one command for local server startup.

**Checkpoint:** empty-but-valid implementations of all five endpoints pass schema tests.

### Phase 1 — state and operational floor

- [ ] Implement SQLite schema, WAL setup, migrations, and repositories.
- [ ] Implement atomic context version handling.
- [ ] Implement health and metadata.
- [ ] Implement suppression reservation and conversation persistence.
- [ ] Add concurrency, restart, and 500 KB tests.

**Checkpoint:** full expanded warmup loads correctly; higher versions win races; health stays fast.

### Phase 2 — one exceptional end-to-end flow

Start with dentistry research digest and customer recall because together they exercise category, merchant, trigger, customer, citation, consent, language, slots, and both send identities.

- [ ] Implement fact ledger and provenance.
- [ ] Implement eligibility and deterministic ranking.
- [ ] Implement the two trigger playbooks.
- [ ] Implement constrained composition, validation, and fallback.
- [ ] Implement tick persistence and full action schema.

**Checkpoint:** both flows score strongly, remain deterministic, and contain no unsupported tokens.

### Phase 3 — generalize across the challenge

- [ ] Add remaining trigger-family playbooks.
- [ ] Add category voice fallbacks and taboo checking.
- [ ] Cover all ten case-study shapes with original output.
- [ ] Add safe handling for unknown trigger kinds and sparse placeholder payloads.
- [ ] Run all 30 generated pairs plus metamorphic variants.

**Checkpoint:** every category and family has a high-quality path; unseen shapes fail safe.

### Phase 4 — replay winner features

- [ ] Implement intent classifier and reply state machine.
- [ ] Add merchant-level auto-reply fingerprints.
- [ ] Implement immediate action handoff after explicit commitment.
- [ ] Implement stop, hostile, wait, in-scope question, and off-topic handlers.
- [ ] Add unknown-conversation and out-of-order-turn recovery.

**Checkpoint:** every five-turn replay scenario passes without unnecessary model calls.

### Phase 5 — judge calibration and resilience

- [ ] Run the official simulator and collect score reasons.
- [ ] Improve decision or fact selection before adjusting prose.
- [ ] Run score distribution three times.
- [ ] Run load, timeout, outage, restart, and cold-start tests.
- [ ] Freeze policy, prompt, and semantic version when gates pass.

**Checkpoint:** score, determinism, latency, and failure-injection targets in section 3 pass.

### Phase 6 — deployment and submission

- [ ] Deploy the exact tested image to Railway with a `/data` persistent volume, one replica, and public HTTPS.
- [ ] Load no seed data in production unless needed for a smoke environment; let the judge be source of truth.
- [ ] Run external preflight from a network outside the host.
- [ ] Confirm the submitted base URL has no endpoint suffix or trailing accidental path.
- [ ] Confirm metadata contains real solo identity and current version.
- [ ] Complete required form fields and submit once the release is frozen.
- [ ] Keep the bot live until magicpin communicates the evaluation result.

**Checkpoint:** the public URL passes three complete preflights separated by at least one health interval.

## 13. Deployment and public preflight

Choose an always-on Docker host that supplies:

- Public HTTPS without manual certificate renewal
- Persistent disk mounted at a fixed path
- Environment-secret management
- Automatic restart on failure
- Logs and basic CPU/memory metrics
- No sleep/scale-to-zero for the submitted service

Suggested production command:

```text
uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1
```

Public preflight should verify:

1. `GET /v1/healthz` returns 200 and valid counts.
2. `GET /v1/metadata` returns real release data.
3. Fresh context inserts are accepted.
4. Duplicate/stale versions do not mutate state.
5. Higher version replaces immediately.
6. A merchant-scoped tick produces the full action schema.
7. A customer-scoped tick honors consent and `merchant_on_behalf`.
8. Engaged reply sends; wait request waits; STOP ends.
9. Repeating the same tick cannot duplicate the suppression key.
10. A process restart preserves context, suppression, and conversation state.
11. Ten concurrent requests remain inside latency targets.
12. Health remains green while model access is intentionally disabled.

Use synthetic preflight IDs with a unique prefix, then remove them through an authenticated admin script or deploy a clean persistent volume before final submission. Do not expose a public reset endpoint.

## 14. Ease-of-use requirements

The repository should be usable by a reviewer or future maintainer with four commands:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[dev]'
docker compose up --build
python scripts\public_preflight.py --base-url http://localhost:8080
```

Also provide:

- `.env.example` with descriptions and safe defaults, never secrets
- One-page `README.md` explaining architecture, deterministic policy, model/fallback, tradeoffs, local run, test, and deployment
- FastAPI `/docs` locally; optionally disable it in public evaluation mode
- A single `scripts/smoke.py` that pushes a minimal category, merchant, customer, and trigger before exercising tick and reply
- Clear structured logs with `request_id`, `trigger_id`, decision score, fact IDs, latency, model/fallback, and outcome
- A release version visible in logs and `/v1/metadata`

The evaluator must not need an API key, auth header, custom path, setup step, or prior seed load to call the public endpoints.

## 15. Submission checklist

### Technical

- [ ] Public base URL is HTTPS and reachable without authentication.
- [ ] All five required endpoints are live.
- [ ] Response schemas and status codes pass internal contract tests.
- [ ] Latest context versions are atomic and persistent.
- [ ] Tick cap, suppression, recipient dedup, consent, expiry, and opt-out work.
- [ ] Reply state machine passes auto-reply, intent, hostile, wait, no, and off-topic scenarios.
- [ ] No generated URL or ungrounded fact appears in evaluation mode.
- [ ] Determinism, latency, load, restart, and model-outage gates pass.
- [ ] Official simulator score targets pass across three runs.
- [ ] Deployment is always-on and monitored.

### Form

- [ ] Full name matches the solo participant.
- [ ] Email is monitored and spelled correctly.
- [ ] Phone number includes the correct country code/format.
- [ ] Submission URL is the public base URL only, for example `https://bot.example.com`.
- [ ] Optional LinkedIn URL is public and valid if supplied.
- [ ] `/v1/metadata` identity matches the submitted identity.
- [ ] No placeholder names, emails, or example URLs remain.

### After submission

- [ ] Do not redeploy or change model/prompt/policy unless fixing a critical outage.
- [ ] Keep synthetic health monitoring active.
- [ ] Watch error rate, latency, restarts, disk availability, and model failures without logging payload contents.
- [ ] Preserve the exact submitted image and configuration for replay/debugging.
- [ ] Keep the service live until the result email arrives.
- [ ] If retrying after a result, make a measured versioned improvement; multiple identical submissions do not help.

## 16. Final product principles

1. **Judgment before language.** Selecting the right moment matters more than elegant copy.
2. **Context is evidence, not inspiration.** Every claim must be traceable.
3. **One message, one job.** Make the next decision obvious and cheap.
4. **Act when the merchant commits.** Never re-qualify a clear yes.
5. **Silence can be correct.** Skip low-value, stale, unsafe, or repetitive triggers.
6. **Fallback is a feature.** A valid grounded template beats a late or imaginative model answer.
7. **Freshness is correctness.** Version changes must immediately change decisions and invalidate caches.
8. **Conversation memory crosses IDs.** Merchant-level state catches auto-replies and fatigue that conversation-only logic misses.
9. **Operational excellence protects the score.** A brilliant bot that sleeps, times out, or loses state cannot win.
10. **Build the policy, not the test set.** The hidden fresh-context evaluation is the real challenge.

## 17. Sources used

- [Official magicpin AI Challenge page](https://partners.magicpin.in/vera/ai-challenge), checked 2026-08-22
- [Official Vera product page](https://partners.magicpin.in/vera/home), used to understand the intended product behavior
- [Official Vera Engage page](https://partners.magicpin.in/vera/engage), used to verify the product emphasis on current business data, brand voice, in-chat conversion, opt-outs, and multi-turn handling
- `challenge-brief.md`
- `challenge-testing-brief.md`
- `engagement-design.md`
- `engagement-research.md`
- `examples/api-call-examples.md`
- `examples/case-studies.md`
- `dataset/generate_dataset.py` and all supplied seed/category files
- `judge_simulator.py`, inspected as executable evaluator evidence rather than relying only on prose
