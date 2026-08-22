from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from app.config import Settings
from app.storage import ContextRecord, Store


HIGH_ACTION_KINDS = {
    "active_planning_intent",
    "regulation_change",
    "supply_alert",
    "appointment_tomorrow",
    "chronic_refill_due",
    "recall_due",
    "renewal_due",
}

CUSTOMER_CONSENT_SCOPES: dict[str, set[str]] = {
    "recall_due": {"recall_reminders", "appointment_reminders"},
    "appointment_tomorrow": {"appointment_reminders"},
    "chronic_refill_due": {"refill_reminders", "delivery_notifications"},
    "trial_followup": {
        "appointment_reminders",
        "bridal_package_followup",
        "kids_program_updates",
        "promotional_offers",
    },
    "wedding_package_followup": {"bridal_package_followup"},
    "customer_lapsed_soft": {"winback_offers", "promotional_offers"},
    "customer_lapsed_hard": {"winback_offers", "promotional_offers"},
}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _scalar_count(value: Any) -> int:
    if isinstance(value, dict):
        return sum(_scalar_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_scalar_count(item) for item in value)
    return int(value not in (None, "", False))


@dataclass(frozen=True, slots=True)
class Candidate:
    trigger: ContextRecord
    merchant: ContextRecord
    category: ContextRecord
    customer: ContextRecord | None
    recipient_id: str
    suppression_key: str
    score: int
    score_reason: str

    @property
    def trigger_payload(self) -> dict[str, Any]:
        return self.trigger.payload

    @property
    def merchant_payload(self) -> dict[str, Any]:
        return self.merchant.payload

    @property
    def category_payload(self) -> dict[str, Any]:
        return self.category.payload

    @property
    def customer_payload(self) -> dict[str, Any] | None:
        return self.customer.payload if self.customer else None


class DecisionEngine:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings

    def build_candidate(self, trigger: ContextRecord, now_iso: str) -> Candidate | None:
        payload = trigger.payload
        merchant_id = payload.get("merchant_id") or payload.get("payload", {}).get(
            "merchant_id"
        )
        if not isinstance(merchant_id, str) or not merchant_id:
            return None

        merchant = self.store.get_context("merchant", merchant_id)
        if merchant is None:
            return None
        category_slug = merchant.payload.get("category_slug")
        if not isinstance(category_slug, str) or not category_slug:
            return None
        category = self.store.get_context("category", category_slug)
        if category is None:
            return None

        trigger_scope = payload.get("scope", "merchant")
        customer: ContextRecord | None = None
        customer_id = payload.get("customer_id")
        if trigger_scope == "customer":
            if not isinstance(customer_id, str) or not customer_id:
                return None
            customer = self.store.get_context("customer", customer_id)
            if customer is None:
                return None
            if customer.payload.get("merchant_id") != merchant_id:
                return None
            if not self._customer_has_consent(payload, customer.payload):
                return None

        recipient_id = customer_id if customer is not None else merchant_id
        if self.store.is_cooled_down(str(recipient_id), now_iso):
            return None

        if self.settings.strict_expiry:
            expiry = _parse_time(payload.get("expires_at"))
            now = _parse_time(now_iso)
            if expiry is not None and now is not None and expiry < now:
                return None

        kind = str(payload.get("kind", "unknown"))
        trigger_body = payload.get("payload", {})
        if not isinstance(trigger_body, dict):
            trigger_body = {}

        # available_triggers is authoritative in evaluation mode. A low-value
        # festival far in the future remains a deliberate abstention.
        days_until = trigger_body.get("days_until")
        if kind == "festival_upcoming" and isinstance(days_until, (int, float)):
            if days_until > 60:
                return None

        urgency = int(payload.get("urgency", 1) or 1)
        explicit_intent = int(kind == "active_planning_intent")
        time_pressure = int(kind in HIGH_ACTION_KINDS)
        specificity = min(4, _scalar_count(trigger_body))
        active_offers = [
            offer
            for offer in merchant.payload.get("offers", [])
            if isinstance(offer, dict) and offer.get("status") == "active"
        ]
        has_slots = int(
            bool(
                trigger_body.get("available_slots")
                or trigger_body.get("next_session_options")
            )
        )
        actionability = min(2, int(bool(active_offers)) + has_slots)
        merchant_relevance = int(bool(merchant.payload.get("identity")))
        history = merchant.payload.get("conversation_history", [])
        fatigue = 0
        if isinstance(history, list) and history:
            recent = history[-1]
            if isinstance(recent, dict) and recent.get("engagement") in {
                "merchant_no_reply",
                "no_reply",
            }:
                fatigue = 1

        score = (
            20 * explicit_intent
            + 3 * max(1, min(5, urgency))
            + 6 * time_pressure
            + 2 * specificity
            + 3 * actionability
            + 2 * merchant_relevance
            - 4 * fatigue
        )
        suppression_key = str(
            payload.get("suppression_key") or f"{kind}:{trigger.context_id}"
        )
        reason = (
            f"kind={kind}; urgency={urgency}; specificity={specificity}; "
            f"actionability={actionability}; fatigue={fatigue}"
        )
        return Candidate(
            trigger=trigger,
            merchant=merchant,
            category=category,
            customer=customer,
            recipient_id=str(recipient_id),
            suppression_key=suppression_key,
            score=score,
            score_reason=reason,
        )

    @staticmethod
    def _customer_has_consent(
        trigger: dict[str, Any], customer: dict[str, Any]
    ) -> bool:
        preferences = customer.get("preferences", {})
        if isinstance(preferences, dict):
            if preferences.get("reminder_opt_in") is False:
                return False
            if preferences.get("channel") in {"none", "none_recorded", None}:
                return False
        consent = customer.get("consent", {})
        if not isinstance(consent, dict) or not consent.get("opted_in_at"):
            return False
        scopes = set(consent.get("scope", []))
        kind = str(trigger.get("kind", ""))
        required = CUSTOMER_CONSENT_SCOPES.get(kind)
        return not required or bool(scopes.intersection(required))

    def select(self, candidates: Iterable[Candidate]) -> list[Candidate]:
        ordered = sorted(
            candidates,
            key=lambda item: (
                -item.score,
                str(item.trigger.payload.get("expires_at", "9999")),
                -item.trigger.version,
                item.trigger.context_id,
            ),
        )
        selected: list[Candidate] = []
        recipients: set[str] = set()
        for candidate in ordered:
            if candidate.recipient_id in recipients:
                continue
            recipients.add(candidate.recipient_id)
            selected.append(candidate)
            if len(selected) >= self.settings.max_actions_per_tick:
                break
        return selected
