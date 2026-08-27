from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any

from app.config import Settings
from app.messages import (
    MessagePlan,
    _active_offer,
    _clean_label,
    _first_name,
    _merchant_name,
    _select_fact_ids,
    build_fact_ledger,
    build_message_plan,
)
from app.models import Fact, LLMComposition, TickAction
from app.policy import Candidate
from app.storage import Store, stable_hash


URL_RE = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
NUMBER_RE = re.compile(r"(?<![\w])[-+]?\d[\d,]*(?:\.\d+)?%?")
# Small time-to-act / effort numbers ("2-min read", "Live in 10 min"). These
# are engagement framing, not factual claims about the merchant, so they are
# whitelisted rather than required to appear in the fact ledger. Restricted to
# minutes/seconds so factual durations (days, weeks) still need provenance.
EFFORT_RE = re.compile(r"\b(\d+)\s*-?\s*(?:min|mins|minute|minutes|sec|secs|second|seconds)\b", re.IGNORECASE)


def _normal_number(value: str) -> str:
    value = value.replace(",", "").lstrip("+")
    if value.startswith("-"):
        sign, rest = "-", value[1:]
    else:
        sign, rest = "", value
    suffix = "%" if rest.endswith("%") else ""
    rest = rest.removesuffix("%")
    try:
        number = float(rest)
        rendered = str(int(number)) if number.is_integer() else str(number)
        return f"{sign}{rendered}{suffix}"
    except ValueError:
        return value


def _allowed_numbers(ledger: list[Fact], cta: str) -> set[str]:
    allowed: set[str] = set()
    for fact in ledger:
        # Labels in the supplied data often encode signed trends with
        # underscores (for example ``cold_cough_demand_-60``). Scan the
        # humanised rendering too so the sign remains grounded.
        for token in NUMBER_RE.findall(f"{fact.rendered} {_clean_label(fact.rendered)}"):
            allowed.add(_normal_number(token))
        if isinstance(fact.value, str):
            try:
                datetime.fromisoformat(fact.value.replace("Z", "+00:00"))
            except ValueError:
                pass
            else:
                # ISO separators sit next to digits, while the outgoing human
                # rendering separates them. Whitelist only components of
                # strings that actually parse as ISO date/time values.
                allowed.update(
                    _normal_number(token)
                    for token in re.findall(r"\d+", fact.value)
                )
        value = fact.value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            allowed.add(_normal_number(str(value)))
            if abs(value) <= 1:
                percent = round(value * 100, 3)
                allowed.add(_normal_number(f"{percent}%"))
                allowed.add(_normal_number(f"{abs(percent)}%"))
    if cta == "multi_choice_slot":
        allowed.update({"1", "2", "3"})
    return allowed


def validate_composition(
    *,
    body: str,
    cta: str,
    used_fact_ids: list[str],
    ledger: list[Fact],
    category: dict[str, Any],
    expected_cta: str,
    previous_body: str | None,
    no_urls: bool,
) -> list[str]:
    errors: list[str] = []
    if not body.strip():
        errors.append("empty body")
    if len(body) > 1200:
        errors.append("body too long")
    if cta != expected_cta:
        errors.append("CTA changed from the approved plan")
    if body.count("?") > 1:
        errors.append("more than one decision question")
    if no_urls and URL_RE.search(body):
        errors.append("URL forbidden in evaluation mode")
    if previous_body and body.strip() == previous_body.strip():
        errors.append("repeated body")

    fact_ids = {fact.fact_id for fact in ledger}
    unknown_ids = sorted(set(used_fact_ids).difference(fact_ids))
    if unknown_ids:
        errors.append(f"unknown fact ids: {unknown_ids}")

    allowed_numbers = _allowed_numbers(ledger, cta)
    allowed_numbers.update(_normal_number(token) for token in EFFORT_RE.findall(body))
    unknown_numbers = sorted(
        {
            _normal_number(token)
            for token in NUMBER_RE.findall(body)
            if _normal_number(token) not in allowed_numbers
        }
    )
    if unknown_numbers:
        errors.append(f"unsupported numbers: {unknown_numbers}")

    voice = category.get("voice", {})
    taboos = voice.get("vocab_taboo", voice.get("taboos", []))
    lower = body.lower()
    for taboo in taboos if isinstance(taboos, list) else []:
        if str(taboo).lower() in lower:
            errors.append(f"category taboo: {taboo}")
    for universal in ("guaranteed", "100% safe", "miracle"):
        if universal in lower:
            errors.append(f"universal taboo: {universal}")
    return errors


def _validation_reason_codes(errors: list[str]) -> str:
    mappings = (
        ("empty body", "empty-body"),
        ("body too long", "body-too-long"),
        ("CTA changed", "cta-changed"),
        ("more than one", "multiple-questions"),
        ("URL forbidden", "url-forbidden"),
        ("repeated body", "repeated-body"),
        ("unknown fact ids", "unknown-fact-ids"),
        ("unsupported numbers", "unsupported-numbers"),
        ("category taboo", "category-taboo"),
        ("universal taboo", "universal-taboo"),
    )
    codes = {
        code
        for error in errors
        for fragment, code in mappings
        if fragment in error
    }
    return ",".join(sorted(codes)) or "other"


class Composer:
    PROMPT_VERSION = "composer_v2"
    POLICY_VERSION = "policy_v1"

    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self._agent: Any = None
        self._model_label = "deterministic-fallback"
        if settings.model_enabled and settings.model_names:
            self._configure_agent()

    @property
    def model_label(self) -> str:
        return self._model_label

    def _configure_agent(self) -> None:
        try:
            from pydantic_ai import Agent, ModelSettings
            from pydantic_ai.models.fallback import FallbackModel

            models = list(self.settings.model_names)
            model: Any = models[0]
            if len(models) > 1:
                model = FallbackModel(models[0], *models[1:])
            self._agent = Agent(
                model,
                output_type=LLMComposition,
                instructions=(
                    "You are Vera, an operator-to-operator growth assistant. The decision and its CTA are "
                    "already fixed. Rewrite ONLY the message body so it is sharp and high-compulsion: lead with "
                    "the single strongest supplied fact, keep every number/name/offer/source exactly as given, "
                    "invent nothing, name a concrete deliverable, and end with one low-friction ask. Keep it under "
                    "roughly 60 words, ask at most one question, and return the fact_ids you actually used."
                ),
                # Gemini 3.x rejects the legacy sampling parameters. Keep the
                # output cap, while letting the stable model use its defaults.
                model_settings=ModelSettings(max_tokens=500, thinking="low"),
                retries={"output": self.settings.model_output_retries},
            )
            self._model_label = ",".join(models)
        except Exception:
            self._agent = None
            self._model_label = "deterministic-fallback"

    async def compose(
        self,
        candidate: Candidate,
        conversation_id: str,
        previous_body: str | None = None,
    ) -> TickAction:
        ledger = build_fact_ledger(candidate)
        plan = build_message_plan(candidate, ledger)
        cache_key = stable_hash(
            {
                "category": candidate.category.payload_hash,
                "merchant": candidate.merchant.payload_hash,
                "trigger": candidate.trigger.payload_hash,
                "customer": candidate.customer.payload_hash if candidate.customer else None,
                "prompt": self.PROMPT_VERSION,
                "policy": self.POLICY_VERSION,
                "model": self.model_label,
            }
        )
        cached = self.store.get_generation(cache_key)
        if cached is not None:
            cached["conversation_id"] = conversation_id
            return TickAction.model_validate(cached)

        body = plan.fallback_body
        cta = plan.brief.cta
        rationale = plan.rationale
        used_fact_ids = plan.used_fact_ids
        composer_name = "deterministic-fallback"
        composer_detail = "agent-unavailable"

        if self._agent is not None:
            composer_detail = "model-attempted"
            prompt = self._model_prompt(plan)
            try:
                result = await asyncio.wait_for(
                    self._agent.run(prompt),
                    timeout=self.settings.model_timeout_seconds,
                )
                output: LLMComposition = result.output
                # The plan owns the CTA; the model only writes the body.
                errors = validate_composition(
                    body=output.body,
                    cta=plan.brief.cta,
                    used_fact_ids=output.used_fact_ids,
                    ledger=ledger,
                    category=candidate.category_payload,
                    expected_cta=plan.brief.cta,
                    previous_body=previous_body,
                    no_urls=self.settings.evaluation_no_urls,
                )
                if not errors:
                    body = " ".join(output.body.split())
                    rationale = output.rationale_summary
                    used_fact_ids = output.used_fact_ids
                    composer_name = self.model_label
                    composer_detail = "validated-structured-output"
                else:
                    composer_detail = "validation:" + _validation_reason_codes(errors)
            except Exception as exc:
                # Expose only the failure class: never credentials, prompts,
                # provider payloads, or model response text.
                composer_detail = f"exception:{type(exc).__name__}"

        fallback_errors = validate_composition(
            body=body,
            cta=cta,
            used_fact_ids=used_fact_ids,
            ledger=ledger,
            category=candidate.category_payload,
            expected_cta=plan.brief.cta,
            previous_body=previous_body,
            no_urls=self.settings.evaluation_no_urls,
        )
        if fallback_errors:
            body = self._minimal_safe_body(candidate)
            cta = "binary_yes_no"
            rationale = f"Minimal grounded fallback after validation: {'; '.join(fallback_errors)}"
            used_fact_ids = _select_fact_ids(ledger, body)
            composer_name = "minimal-safe-fallback"
            composer_detail = "fallback-validation:" + _validation_reason_codes(
                fallback_errors
            )

        action = TickAction(
            conversation_id=conversation_id,
            merchant_id=candidate.merchant.context_id,
            customer_id=(
                str(candidate.trigger_payload.get("customer_id"))
                if candidate.trigger_payload.get("customer_id")
                else None
            ),
            send_as=plan.brief.send_as,
            trigger_id=candidate.trigger.context_id,
            template_name=plan.template_name,
            template_params=plan.template_params,
            body=body,
            cta=cta,
            suppression_key=candidate.suppression_key,
            rationale=rationale,
            composer_source=composer_name,
            composer_detail=composer_detail,
        )
        cached_response = action.model_dump(mode="json")
        cached_response["composer_detail"] = composer_detail
        self.store.save_generation(
            cache_key=cache_key,
            response=cached_response,
            selected_fact_ids=used_fact_ids,
            composer=composer_name,
        )
        return action

    @staticmethod
    def _model_prompt(plan: MessagePlan) -> str:
        brief = plan.brief.model_dump(mode="json")
        # Full values are supplied as data, but the decision fields are prominent.
        return json.dumps(
            {
                "task": "Realize the approved message brief without changing its decision.",
                "brief": brief,
                "fallback_shape": plan.fallback_body,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _minimal_safe_body(candidate: Candidate) -> str:
        owner = _first_name(candidate)
        kind = _clean_label(candidate.trigger_payload.get("kind") or "current signal")
        return (
            f"{owner}, the current {kind} signal is ready to review. "
            "Want me to prepare one grounded next step?"
        )
