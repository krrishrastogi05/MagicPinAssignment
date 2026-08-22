from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from app.models import ReplyRequest, ReplyResponse
from app.storage import Store, stable_hash


STOP_RE = re.compile(
    r"\b(stop|unsubscribe|remove me|do not (?:message|contact)|don't (?:message|contact)|"
    r"dont (?:message|contact)|never message|spam|useless|not interested)\b",
    re.IGNORECASE,
)
AUTO_RE = re.compile(
    r"(thank you for (?:contacting|reaching|messaging)|our team will (?:respond|reply)|"
    r"automated (?:reply|response|assistant)|business hours|away (?:right now|message)|"
    r"we(?:'ll| will) get back to you|hamari team tak)",
    re.IGNORECASE,
)
ACCEPT_RE = re.compile(
    r"\b(yes|yep|yeah|ok(?:ay)?|sure|go ahead|lets do it|let's do it|do it|"
    r"proceed|send it|start|join|interested|confirm|haan|ha|chalo|kar do)\b",
    re.IGNORECASE,
)
WAIT_RE = re.compile(
    r"\b(later|tomorrow|next week|busy|call me|remind me|not now|baad mein|kal)\b",
    re.IGNORECASE,
)
HARD_NO_RE = re.compile(
    r"\b(no thanks|no thank you|not now|maybe later|not required|don't need|dont need|nahi)\b",
    re.IGNORECASE,
)
QUESTION_RE = re.compile(r"\?|\b(what|why|how|when|where|which|price|cost|kitna|kaise)\b", re.IGNORECASE)
OFF_TOPIC_RE = re.compile(
    r"\b(gst|income tax|cricket score|weather|loan|politics|aadhaar)\b",
    re.IGNORECASE,
)


def _normalise_message(message: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", message.lower()).split())


def _is_hinglish(message: str) -> bool:
    return bool(
        re.search(
            r"\b(haan|nahi|kya|kaise|kar do|bhejo|chalo|theek|kal|baad|kitna)\b",
            message,
            re.IGNORECASE,
        )
    )


class ReplyEngine:
    """Small deterministic state machine for the evaluator's highest-risk turns."""

    def __init__(self, store: Store):
        self.store = store

    def handle(self, request: ReplyRequest) -> ReplyResponse:
        conversation = self.store.get_conversation(request.conversation_id)
        merchant_id = request.merchant_id or (
            conversation.merchant_id if conversation is not None else None
        )
        customer_id = request.customer_id or (
            conversation.customer_id if conversation is not None else None
        )
        merchant_id = merchant_id or "unknown-merchant"

        if conversation is None:
            self.store.create_conversation(
                conversation_id=request.conversation_id,
                merchant_id=merchant_id,
                customer_id=customer_id,
                trigger_id=None,
                state="received",
                last_body=None,
                context={},
            )
            conversation = self.store.get_conversation(request.conversation_id)

        self.store.append_turn(
            conversation_id=request.conversation_id,
            turn_number=request.turn_number,
            from_role=request.from_role,
            message=request.message,
            received_at=request.received_at,
        )

        message = request.message.strip()
        normalised = _normalise_message(message)
        context = conversation.context if conversation is not None else {}

        if STOP_RE.search(message):
            self.store.set_cooldown(
                customer_id or merchant_id,
                reason="explicit_stop_or_hostile",
                permanent=True,
            )
            return self._finish(
                request,
                action="end",
                body="Understood - sorry for the interruption. We won't message you again.",
                rationale="Explicit stop or hostile signal; consent and safety override all engagement goals.",
            )

        if AUTO_RE.search(message):
            fingerprint = stable_hash(normalised)
            seen = self.store.record_fingerprint(merchant_id, fingerprint)
            if seen >= 3:
                return self._finish(
                    request,
                    action="end",
                    body=None,
                    rationale="The same merchant-level auto-reply fingerprint repeated three times; ending the loop.",
                    auto_reply_count=seen,
                )
            return self._finish(
                request,
                action="wait",
                wait_seconds=14_400 if seen == 1 else 86_400,
                rationale=f"Detected a canned auto-reply ({seen}/3); waiting without polluting the conversation.",
                auto_reply_count=seen,
            )

        # Commitment must outrank questions: "yes, what next?" is action intent.
        if ACCEPT_RE.search(message):
            if _is_hinglish(message):
                body = "Done - main abhi draft taiyar kar rahi hoon. Next, aap bas final copy CONFIRM kar dena."
            else:
                body = "Done - I'm preparing the draft now. Next, reply CONFIRM after you review the final copy."
            return self._finish(
                request,
                action="send",
                body=body,
                cta="confirm",
                rationale="The recipient committed; switched immediately from qualification to execution.",
            )

        if WAIT_RE.search(message):
            seconds = 86_400 if re.search(r"tomorrow|\bkal\b", message, re.I) else 14_400
            return self._finish(
                request,
                action="wait",
                wait_seconds=seconds,
                rationale="The recipient requested time; respecting the requested pause.",
            )

        if HARD_NO_RE.search(message):
            until = (datetime.now(UTC) + timedelta(days=30)).isoformat().replace("+00:00", "Z")
            self.store.set_cooldown(
                customer_id or merchant_id,
                reason="soft_decline",
                until_at=until,
            )
            return self._finish(
                request,
                action="end",
                body="Understood. I'll close this here.",
                rationale="Clear decline; ending gracefully and applying a 30-day cooldown.",
            )

        if OFF_TOPIC_RE.search(message):
            topic = context.get("goal") or "the business task in this conversation"
            return self._finish(
                request,
                action="send",
                body=f"I don't have verified context for that. I can help with {topic}; reply CONTINUE to proceed.",
                cta="continue",
                rationale="Declined to fabricate an off-topic answer and redirected to the grounded task.",
            )

        if QUESTION_RE.search(message):
            offer = context.get("active_offer")
            merchant_name = context.get("merchant_name") or "the business"
            if offer and re.search(r"price|cost|kitna|offer", message, re.I):
                body = f"The current supplied offer from {merchant_name} is {offer}. Reply CONFIRM and I'll prepare the next step."
            else:
                body = (
                    "I don't have enough verified context to answer that precisely. "
                    "Reply CONTINUE and I'll proceed using only the details already supplied."
                )
            return self._finish(
                request,
                action="send",
                body=body,
                cta="continue",
                rationale="Answered from saved conversation facts or stated the limitation instead of guessing.",
            )

        body = (
            "I may have missed your intent. Reply YES to continue, LATER to pause, or STOP to end."
        )
        return self._finish(
            request,
            action="send",
            body=body,
            cta="multi_choice",
            rationale="Ambiguous response; used one bounded clarification with an explicit exit.",
        )

    def _finish(
        self,
        request: ReplyRequest,
        *,
        action: str,
        rationale: str,
        body: str | None = None,
        cta: str | None = None,
        wait_seconds: int | None = None,
        auto_reply_count: int | None = None,
    ) -> ReplyResponse:
        state = {"send": "active", "wait": "waiting", "end": "ended"}[action]
        self.store.update_conversation(
            request.conversation_id,
            state=state,
            last_body=body,
            auto_reply_count=auto_reply_count,
        )
        return ReplyResponse(
            action=action,
            body=body,
            cta=cta,
            wait_seconds=wait_seconds,
            rationale=rationale,
        )
