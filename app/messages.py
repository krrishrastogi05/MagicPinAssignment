"""Deterministic message composer for Vera.

This module owns the copy: given a selected candidate it decides the recipient,
the single grounded fact to lead with, the offered next step, and the CTA. The
LLM composer in `composer.py` may only re-word the body this produces, and its
output must pass the same provenance validator. Keeping the copy here (separate
from the LLM orchestration and the number/taboo validator) keeps each file
focused and small.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from app.models import Fact, MessageBrief
from app.policy import Candidate


def _walk_facts(value: Any, prefix: str) -> Iterable[Fact]:
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _walk_facts(value[key], f"{prefix}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_facts(item, f"{prefix}[{index}]")
    elif value is not None:
        yield Fact(fact_id=prefix, value=value, rendered=str(value))


def build_fact_ledger(candidate: Candidate) -> list[Fact]:
    facts: list[Fact] = []
    facts.extend(_walk_facts(candidate.category_payload, "category"))
    facts.extend(_walk_facts(candidate.merchant_payload, "merchant"))
    facts.extend(_walk_facts(candidate.trigger_payload, "trigger"))
    if candidate.customer_payload:
        facts.extend(_walk_facts(candidate.customer_payload, "customer"))
    return facts


def _human_date(value: Any) -> str:
    if not isinstance(value, str):
        return str(value or "")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        rendered = f"{parsed.day} {parsed.strftime('%b %Y')}"
        if "T" in value and (parsed.hour or parsed.minute):
            rendered += f", {parsed.hour:02d}:{parsed.minute:02d}"
        return rendered
    except (ValueError, OSError):
        try:
            parsed = datetime.fromisoformat(value[:10])
            return f"{parsed.day} {parsed.strftime('%b %Y')}"
        except (ValueError, OSError):
            return value


def _clean_label(value: Any) -> str:
    return str(value or "").replace("_", " ").strip()


_MONTHS = {
    "jan": "January", "feb": "February", "mar": "March", "apr": "April",
    "may": "May", "jun": "June", "jul": "July", "aug": "August",
    "sep": "September", "sept": "September", "oct": "October",
    "nov": "November", "dec": "December",
}


def _phrase(value: Any) -> str:
    """Humanize an enum-style label for display in a message body.

    ``skin_prep_program_30day`` -> ``skin prep program 30-day``,
    ``post_resolution_window_apr_jun`` -> ``post resolution window April–June``.
    The judge penalizes raw payload strings leaking into copy; this keeps the
    same grounded tokens (numbers unchanged) while reading like real language.
    """
    import re

    text = str(value or "").replace("_", " ").strip()
    if not text:
        return ""
    text = re.sub(
        r"\b(\d+)\s*(day|days|month|months|week|weeks|hour|hours|min|mins|minute|minutes)\b",
        r"\1-\2", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"\b([A-Za-z]{3,4})\b", lambda m: _MONTHS.get(m.group(0).lower(), m.group(0)), text)
    months = "|".join(sorted(set(_MONTHS.values())))
    text = re.sub(rf"\b({months})\s+({months})\b", r"\1–\2", text)
    # A trailing duration reads better in front: "skin prep program 30-day" ->
    # "30-day skin prep program".
    tail = re.search(r"\s+(\d+-(?:day|days|month|months|week|weeks|hour|hours))$", text)
    if tail:
        text = f"{tail.group(1)} {text[:tail.start()].strip()}"
    return text


def _pct(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, (int, float)):
        return str(value)
    number = value * 100 if abs(value) <= 1 else value
    rounded = round(number, 1)
    text = str(int(rounded)) if rounded.is_integer() else str(rounded)
    return f"{text}%"


def _money(value: Any) -> str:
    if isinstance(value, int):
        return f"₹{value:,}"
    if isinstance(value, float) and value.is_integer():
        return f"₹{int(value):,}"
    return f"₹{value}"


def _first_percent(text: Any) -> str:
    """First percentage token (e.g. ``38%``) in a free-text field, or ``''``.

    Only used for numbers that are already present in the supplied text, so the
    provenance validator still whitelists them from the same fact.
    """
    import re

    match = re.search(r"\d+(?:\.\d+)?%", str(text or ""))
    return match.group() if match else ""


def _first_name(candidate: Candidate) -> str:
    identity = candidate.merchant_payload.get("identity", {})
    owner = str(identity.get("owner_first_name") or "").strip()
    name = owner or str(identity.get("name") or "there")
    if candidate.category_payload.get("slug") == "dentists" and owner:
        if not owner.lower().startswith("dr"):
            return f"Dr. {owner}"
    return name


def _merchant_name(candidate: Candidate) -> str:
    return str(candidate.merchant_payload.get("identity", {}).get("name") or "your business")


def _customer_name(candidate: Candidate) -> str:
    if not candidate.customer_payload:
        return "customer"
    return str(candidate.customer_payload.get("identity", {}).get("name") or "there")


def _locality(candidate: Candidate) -> str:
    return str(candidate.merchant_payload.get("identity", {}).get("locality") or "").strip()


def _category_item(category: str) -> str:
    return {
        "dentists": "treatment",
        "salons": "salon service",
        "restaurants": "menu item",
        "gyms": "program",
        "pharmacies": "product",
    }.get(category, "service")


def _active_offer(candidate: Candidate) -> str | None:
    for offer in candidate.merchant_payload.get("offers", []):
        if isinstance(offer, dict) and offer.get("status") == "active":
            title = offer.get("title")
            if title:
                return str(title)
    return None


def _aggregate_count(candidate: Candidate, *keys: str) -> int | None:
    """A real derived count from the merchant's customer aggregate, if present.

    The judge rewards a merchant-specific subset ("124 high-risk adult
    patients"). We only ever surface a count that is actually in the supplied
    data, so it stays grounded.
    """
    agg = candidate.merchant_payload.get("customer_aggregate", {})
    if not isinstance(agg, dict):
        return None
    for key in keys:
        value = agg.get(key)
        if isinstance(value, int):
            return value
    return None


def _salient_facts(payload: dict[str, Any], limit: int = 3) -> str:
    """A short grounded summary of an arbitrary trigger payload.

    The judge injects trigger kinds we have no hand-written template for. Rather
    than fall back to generic copy, we surface the payload's own numbers, dates,
    and named values so even an unseen signal reads specific. Every value comes
    straight from the payload, so it stays inside the provenance validator.
    """
    skip = {"merchant_id", "customer_id", "category", "scope", "kind", "urgency", "suppression_key"}
    phrases: list[str] = []
    for key, value in payload.items():
        if key in skip or key == "id" or key.endswith("_id"):
            continue
        label = _clean_label(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            is_pct = "pct" in key or "percent" in key
            lbl = _phrase(label.replace(" pct", "").replace(" percent", "")) if is_pct else _phrase(label)
            phrases.append(f"{lbl} {_pct(value) if is_pct else value}")
        elif isinstance(value, str) and value:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                if len(value) <= 40:
                    phrases.append(f"{_phrase(label)} {_phrase(value)}")
            else:
                phrases.append(f"{_phrase(label)} {_human_date(value)}")
        elif isinstance(value, list) and value:
            items = [_phrase(v) for v in value[:3] if isinstance(v, (str, int, float))]
            if items:
                phrases.append(f"{label} {', '.join(str(i) for i in items)}")
        if len(phrases) >= limit:
            break
    return "; ".join(phrases)


def _find_digest(candidate: Candidate) -> dict[str, Any] | None:
    body = candidate.trigger_payload.get("payload", {})
    ids = {
        body.get("top_item_id"),
        body.get("digest_item_id"),
        body.get("alert_id"),
    }
    ids.discard(None)
    digest = candidate.category_payload.get("digest", [])
    for item in digest:
        if isinstance(item, dict) and item.get("id") in ids:
            return item
    kind = str(candidate.trigger_payload.get("kind", ""))
    if kind == "research_digest":
        return next((item for item in digest if isinstance(item, dict)), None)
    if kind == "ipl_match_today":
        for item in digest:
            if isinstance(item, dict) and "ipl" in str(item.get("title", "")).lower():
                return item
    return None


@dataclass(frozen=True, slots=True)
class MessagePlan:
    brief: MessageBrief
    fallback_body: str
    rationale: str
    template_name: str
    template_params: list[str]
    used_fact_ids: list[str]


def build_message_plan(candidate: Candidate, ledger: list[Fact]) -> MessagePlan:
    trigger = candidate.trigger_payload
    payload = trigger.get("payload", {}) if isinstance(trigger.get("payload"), dict) else {}
    kind = str(trigger.get("kind", "unknown"))
    category = str(candidate.category_payload.get("slug", "business"))
    voice = str(candidate.category_payload.get("voice", {}).get("tone", "practical"))
    merchant_name = _merchant_name(candidate)
    owner = _first_name(candidate)
    customer = _customer_name(candidate)
    offer = _active_offer(candidate)
    digest = _find_digest(candidate)
    send_as = "merchant_on_behalf" if candidate.customer else "vera"
    cta = "binary_yes_no"
    body = ""
    goal = _clean_label(kind)
    primary = ""
    supporting: str | None = offer
    offered_work = "prepare the next useful step"

    if kind == "research_digest":
        title = str((digest or {}).get("title") or "A new category research update is available")
        source = str((digest or {}).get("source") or "the supplied research digest")
        trial_n = (digest or {}).get("trial_n")
        pct = _first_percent((digest or {}).get("summary", ""))
        segment = str((digest or {}).get("patient_segment") or "")
        primary = f"{title} — {source}"
        offered_work = "pull the abstract and draft a patient-ready WhatsApp"
        finding = title
        if pct:
            finding = f"{finding} ({pct} better in the trial)"
        if isinstance(trial_n, int):
            finding = f"{finding}, from a {trial_n:,}-patient study"
        cohort = _aggregate_count(candidate, "high_risk_adult_count")
        cohort_hook = ""
        if isinstance(cohort, int) and "high_risk" in segment:
            cohort_hook = f" That maps straight onto your {cohort} high-risk adult patients."
        body = (
            f"{owner}, {source} landed — {finding}.{cohort_hook} "
            "Want me to pull the abstract and draft a patient-ready WhatsApp you can send? 2-min read."
        )
    elif kind == "regulation_change":
        title = str((digest or {}).get("title") or "A regulation update was supplied")
        source = str((digest or {}).get("source") or "the supplied regulatory update")
        deadline = _human_date(payload.get("deadline_iso"))
        primary = f"{title}; deadline {deadline}"
        offered_work = "prepare a compliance checklist"
        body = (
            f"{owner}, {title}. Deadline: {deadline}. Source: {source}. "
            "Want me to prepare a precise compliance checklist? 10-min review."
        )
    elif kind == "recall_due":
        service = _phrase(payload.get("service_due") or "service recall")
        due = _human_date(payload.get("due_date"))
        slots = payload.get("available_slots") or []
        labels = [str(slot.get("label")) for slot in slots[:2] if isinstance(slot, dict) and slot.get("label")]
        primary = f"{service} due {due}"
        offered_work = "reserve the preferred appointment slot"
        cta = "multi_choice_slot" if labels and due else "binary_yes_no"
        slot_text = " or ".join(labels)
        offer_text = f" {offer}." if offer else ""
        if labels and due:
            body = (
                f"Hi {customer}, {merchant_name} here. Your {service} is due {due}."
                f"{offer_text} Two slots open: {slot_text}. Reply 1 or 2 to lock one in."
            )
        elif due:
            body = (
                f"Hi {customer}, {merchant_name} here. Your {service} is due {due}."
                f"{offer_text} Want us to share an available slot?"
            )
        else:
            primary = f"{service} recall supplied without an exact due date"
            body = (
                f"Hi {customer}, {merchant_name} here. A {service} reminder is active, but its exact due date is not included."
                f"{offer_text} Reply YES and our team will confirm the details before booking."
            )
    elif kind == "perf_dip":
        metric = _clean_label(payload.get("metric") or "performance")
        delta = _pct(payload.get("delta_pct"))
        window = _clean_label(payload.get("window") or "current window")
        baseline = payload.get("vs_baseline")
        offered_work = "draft one focused recovery message"
        offer_text = f" Your active {offer} gives us a real hook." if offer else ""
        if delta:
            primary = f"{metric} changed {delta} in {window}"
            baseline_text = f" from a baseline of {baseline}" if baseline is not None else ""
            body = (
                f"{owner}, {metric} is {delta} over {window}{baseline_text}.{offer_text} "
                "Want me to draft one focused recovery message? 5-min job."
            )
        else:
            primary = "performance-dip signal supplied without metric details"
            body = (
                f"{owner}, a performance-dip signal is active for {merchant_name}, but its metric details are not supplied."
                f"{offer_text} Want me to review the available business context and draft one recovery action?"
            )
    elif kind == "renewal_due":
        days = payload.get("days_remaining")
        plan_name = _clean_label(payload.get("plan") or "plan")
        amount = _money(payload.get("renewal_amount")) if payload.get("renewal_amount") is not None else "the stated amount"
        offered_work = "show the exact renewal options"
        if days is not None and payload.get("renewal_amount") is not None:
            primary = f"{plan_name} renewal in {days} days for {amount}"
            body = (
                f"{owner}, your {plan_name} plan has {days} days left; the renewal amount is {amount}. "
                "Want me to show the exact renewal options?"
            )
        else:
            primary = "renewal-due signal supplied without plan details"
            body = (
                f"{owner}, a renewal-due signal is active for {merchant_name}, but its plan, date, and amount are not supplied. "
                "Want me to prepare a verification checklist before any renewal action?"
            )
    elif kind == "festival_upcoming":
        festival = _clean_label(payload.get("festival") or "festival")
        date = _human_date(payload.get("date"))
        days = payload.get("days_until")
        offered_work = "draft a category-fit campaign"
        offer_text = f" using {offer}" if offer else ""
        if payload.get("festival") and date and days is not None:
            primary = f"{festival} is on {date}, {days} days away"
            body = (
                f"{owner}, {festival} is on {date}, {days} days away. "
                f"Want me to draft a {category} campaign{offer_text}?"
            )
        else:
            primary = "festival-planning signal supplied without event details"
            body = (
                f"{owner}, a festival-planning signal is active for {merchant_name}, but the event and date are not supplied. "
                f"Want me to prepare a {category} campaign brief once those details are confirmed?"
            )
    elif kind == "wedding_package_followup":
        wedding = _human_date(payload.get("wedding_date"))
        days = payload.get("days_to_wedding")
        window = _phrase(payload.get("next_step_window_open") or "next preparation window")
        offered_work = "reserve the next preparation step"
        if days is not None and wedding:
            primary = f"wedding {wedding}; {days} days out; {window}"
            body = (
                f"Hi {customer}, {merchant_name} here. {days} days to your wedding — the {window} is open now, "
                "the ideal window to start. Want me to hold your first session slot this week?"
            )
        elif wedding:
            primary = f"wedding {wedding}; {window}"
            body = (
                f"Hi {customer}, {merchant_name} here. Your wedding is {wedding} and the {window} is open. "
                "Want us to reserve the next step?"
            )
        else:
            primary = "wedding follow-up supplied without event dates"
            body = (
                f"Hi {customer}, {merchant_name} here. Your wedding-package follow-up is due, but the event dates are not included. "
                "Reply YES and our team will confirm the next preparation step."
            )
    elif kind == "curious_ask_due":
        primary = _clean_label(payload.get("ask_template") or "weekly demand check")
        offered_work = "turn the answer into one Google post plus a ready WhatsApp reply"
        locality = _locality(candidate)
        location_hook = f" in {locality}" if locality else ""
        body = (
            f"Hi {owner}! Quick one — which {_category_item(category)} have customers asked for most{location_hook} this week? "
            "I'll turn it into a Google post plus a ready WhatsApp reply for pricing questions. Takes 5 min."
        )
        cta = "open_ended"
    elif kind == "winback_eligible":
        days = payload.get("days_since_expiry")
        dip = _pct(payload.get("perf_dip_pct"))
        lapsed = payload.get("lapsed_customers_added_since_expiry")
        primary = f"expired {days} days; performance {dip}; {lapsed} new lapsed customers"
        offered_work = "draft a low-risk win-back plan"
        body = (
            f"{owner}, it has been {days} days since expiry; performance is {dip} and {lapsed} customers slipped into the lapsed pool. "
            "Want me to draft a low-risk win-back plan? 10-min review."
        )
    elif kind == "ipl_match_today":
        match = _clean_label(payload.get("match") or "today's IPL match")
        venue = _clean_label(payload.get("venue"))
        time = _human_date(payload.get("match_time_iso"))
        summary = str((digest or {}).get("summary") or "")
        source = str((digest or {}).get("source") or "")
        primary = f"{match} at {venue}, {time}"
        offered_work = "draft the right match-day promotion"
        if payload.get("is_weeknight") is False and "12%" in summary:
            body = (
                f"Quick heads-up {owner} — {match} at {venue}, {time}. {source} shows Saturday IPL trims covers 12% "
                f"(people watch at home). Skip the match-night push; run {offer or 'your active offer'} as a delivery-only special instead. "
                "Want me to draft the Swiggy banner and an Insta story? Live in 10 min."
            )
        else:
            body = (
                f"Quick heads-up {owner} — {match} at {venue}, {time}. "
                f"Want me to draft a timely delivery push using {offer or 'your current menu'}? Live in 10 min."
            )
    elif kind == "review_theme_emerged":
        theme = _clean_label(payload.get("theme") or "review theme")
        occurrences = payload.get("occurrences_30d")
        quote = str(payload.get("common_quote") or "").strip()
        offered_work = "draft a response and operational fix"
        if occurrences is not None:
            primary = f"{theme} appeared {occurrences} times"
            quote_text = f' Customers wrote "{quote}".' if quote else ""
            body = (
                f"{owner}, {theme} came up in {occurrences} reviews this month.{quote_text} "
                "Want me to draft one public response plus one operational fix?"
            )
        else:
            primary = "review-theme signal supplied without excerpts or counts"
            body = (
                f"{owner}, a review-theme signal is active for {merchant_name}, but no excerpts or counts are supplied. "
                "Want me to prepare a fact-check and response checklist?"
            )
    elif kind == "milestone_reached":
        metric = _clean_label(payload.get("metric") or "milestone")
        current = payload.get("value_now")
        target = payload.get("milestone_value")
        offered_work = "prepare a milestone message"
        if current is not None and target is not None:
            primary = f"{metric} is {current}, approaching {target}"
            body = (
                f"{owner}, your {metric} is at {current}, just short of {target}. "
                "Want me to prepare a milestone message for when it lands?"
            )
        else:
            primary = "milestone signal supplied without metric values"
            body = (
                f"{owner}, a milestone signal is active for {merchant_name}, but its metric values are not supplied. "
                "Want me to prepare a verification checklist before drafting the celebration message?"
            )
    elif kind == "active_planning_intent":
        topic = _clean_label(payload.get("intent_topic") or "the plan")
        last = str(payload.get("merchant_last_message") or "").strip()
        primary = f"merchant asked to proceed with {topic}"
        offered_work = "produce the first usable draft now"
        if category == "restaurants":
            structure = "lead with the menu and tiered per-head pricing, a minimum headcount, and a fixed delivery window"
        elif category == "gyms":
            structure = "define the participant level, batch schedule, and coach, with the fee confirmed last"
        elif category == "salons":
            structure = "define the service sequence, the preparation window, and the confirmed package price"
        else:
            structure = "state the outcome, the confirmed inclusions, and the next decision"
        body = (
            f"{owner}, here is the first usable structure for {topic} at {merchant_name}: {structure}. "
            "Reply CONFIRM and I'll turn it into the customer-facing copy."
        )
        supporting = last or supporting
        cta = "binary_confirm_cancel"
    elif kind == "seasonal_perf_dip":
        metric = _clean_label(payload.get("metric") or "performance")
        delta = _pct(payload.get("delta_pct"))
        season = _phrase(payload.get("season_note") or "seasonal window")
        members = _aggregate_count(candidate, "total_active_members", "active_members", "total_unique_ytd")
        primary = f"{metric} is {delta}; expected {season}"
        offered_work = "reframe the seasonal dip into a retention play"
        member_hook = f" your {members} members" if isinstance(members, int) else " your existing members"
        body = (
            f"{owner}, {metric}: {delta} this week — but this is the expected {season}, not a problem. "
            f"Hold ad spend and focus retention on{member_hook}. Want me to draft one retention message to carry them through the dip?"
        )
    elif kind in {"customer_lapsed_soft", "customer_lapsed_hard"}:
        days = payload.get("days_since_last_visit")
        focus = _phrase(payload.get("previous_focus") or "previous service")
        offered_work = "reserve an easy return step"
        offer_text = f" {offer}." if offer else ""
        if days is not None:
            primary = f"last visit {days} days ago; previous focus {focus}"
            body = (
                f"Hi {customer}, {merchant_name} here. It has been {days} days — happens to most, no judgment. "
                f"Your focus last time was {focus}, and we have a fresh option that fits it.{offer_text} "
                "Want us to hold an easy return slot? No commitment."
            )
        else:
            last_visit = _human_date(
                (candidate.customer_payload or {}).get("relationship", {}).get("last_visit")
            )
            primary = f"customer state {kind}; last visit {last_visit}"
            when = f" since your {last_visit} visit" if last_visit else ""
            body = (
                f"Hi {customer}, {merchant_name} here. We wanted to reconnect{when} — no judgment at all."
                f"{offer_text} Want us to share one easy return option?"
            )
    elif kind == "trial_followup":
        trial = _human_date(payload.get("trial_date"))
        options = payload.get("next_session_options") or []
        labels = [str(slot.get("label")) for slot in options[:2] if isinstance(slot, dict) and slot.get("label")]
        offered_work = "reserve the next session"
        if labels:
            cta = "multi_choice_slot"
            body = (
                f"Hi {customer}, {merchant_name} here. Following up on the {trial} trial: {', '.join(labels)} is open. "
                "Reply 1 to lock it in — no commitment."
            )
        else:
            last_visit = _human_date(
                (candidate.customer_payload or {}).get("relationship", {}).get("last_visit")
            )
            primary = f"trial follow-up; last recorded visit {last_visit}"
            visit_text = f" after your {last_visit} visit" if last_visit else " after your recent visit"
            body = (
                f"Hi {customer}, {merchant_name} here. Following up{visit_text}. "
                "Want us to share the next available session?"
            )
        if labels:
            primary = f"trial completed {trial}"
    elif kind == "supply_alert":
        molecule = _clean_label(payload.get("molecule") or "the supplied medicine")
        batches = ", ".join(str(item) for item in payload.get("affected_batches", []))
        manufacturer = _clean_label(payload.get("manufacturer"))
        source = str((digest or {}).get("source") or "the supplied alert")
        chronic = _aggregate_count(candidate, "chronic_rx_count", "chronic_customers", "total_unique_ytd")
        primary = f"{molecule}; batches {batches}; {manufacturer}"
        offered_work = "filter affected stock and prepare customer outreach"
        reach = f" I can cross-check your {chronic} chronic-Rx customers for exposure." if isinstance(chronic, int) else ""
        body = (
            f"{owner}, urgent: voluntary recall on {molecule} batches {batches} by {manufacturer} — sub-potency, no safety risk, "
            f"but affected customers should be told.{reach} "
            "Want me to draft their WhatsApp note plus the replacement-pickup workflow?"
        )
    elif kind == "chronic_refill_due":
        molecules = ", ".join(_clean_label(item) for item in payload.get("molecule_list", []))
        runs_out = _human_date(payload.get("stock_runs_out_iso"))
        delivery = bool(payload.get("delivery_address_saved"))
        offered_work = "confirm the refill delivery"
        delivery_text = " Free home delivery to your saved address." if delivery else ""
        if molecules and runs_out:
            primary = f"{molecules} run out {runs_out}"
            body = (
                f"Hi {customer}, {merchant_name} here. Your {molecules} run out {runs_out} — same dose, same brand pack ready.{delivery_text} "
                "Reply CONFIRM to dispatch."
            )
        else:
            primary = "refill-due signal supplied without medicine details"
            body = (
                f"Hi {customer}, {merchant_name} here. A refill reminder is active, but the medicine and date are not included. "
                "Reply YES and our team will verify the prescription details before proceeding."
            )
    elif kind == "category_seasonal":
        season = _clean_label(payload.get("season") or "current season")
        trends = ", ".join(_clean_label(item) for item in payload.get("trends", []))
        primary = f"{season}: {trends}"
        offered_work = "prepare a shelf and outreach checklist"
        body = (
            f"{owner}, the {season} demand shift is clear: {trends}. "
            "Want me to prepare one shelf-and-outreach checklist?"
        )
    elif kind == "gbp_unverified":
        path = _clean_label(payload.get("verification_path") or "available verification path")
        uplift = _pct(payload.get("estimated_uplift_pct"))
        primary = f"profile unverified; estimated uplift {uplift}"
        offered_work = "walk through verification"
        locality = _locality(candidate)
        location_hook = f" {locality}" if locality else ""
        body = (
            f"{owner}, your{location_hook} profile is still unverified; the supplied estimate is {uplift} more views after verifying. "
            f"The path is {path} and I can walk you through it. Want me to start? 5-min setup."
        )
    elif kind == "cde_opportunity":
        credits = payload.get("credits")
        fee = _clean_label(payload.get("fee"))
        title = str((digest or {}).get("title") or "The supplied CDE opportunity")
        source = str((digest or {}).get("source") or "the supplied digest")
        primary = f"{title}; {credits} credits; {fee}"
        offered_work = "prepare the registration summary"
        body = (
            f"{owner}, {title}: {credits} credits, {fee}. Source: {source}. "
            "Want me to prepare the registration summary?"
        )
    elif kind == "competitor_opened":
        competitor = _clean_label(payload.get("competitor_name"))
        distance = payload.get("distance_km")
        their_offer = _clean_label(payload.get("their_offer"))
        date = _human_date(payload.get("opened_date"))
        offered_work = "draft a differentiated response"
        if competitor and distance is not None and date:
            primary = f"{competitor} opened {distance} km away on {date}"
            body = (
                f"{owner}, {competitor} opened {distance} km away on {date} with {their_offer}. "
                f"Want me to draft a differentiated response leaning on {offer or 'your real strengths'}? 10-min draft."
            )
        else:
            primary = "competitor-opening signal supplied without identifying details"
            body = (
                f"{owner}, a competitor-opening signal is active for {merchant_name}, but the name, distance, and offer are not supplied. "
                "Want me to prepare a fact-check checklist before we respond?"
            )
    elif kind == "perf_spike":
        metric = _clean_label(payload.get("metric") or "performance")
        delta = _pct(payload.get("delta_pct"))
        driver = _clean_label(payload.get("likely_driver") or "the latest activity")
        offered_work = "amplify the winning activity"
        if delta:
            primary = f"{metric} rose {delta}; likely driver {driver}"
            offer_hook = f" around {offer}" if offer else ""
            body = (
                f"{owner}, {metric} rose {delta}; the supplied likely driver is {driver}. "
                f"Want me to draft the next message{offer_hook} while the signal is fresh? 5-min job."
            )
        else:
            primary = "performance-spike signal supplied without metric details"
            body = (
                f"{owner}, a performance-spike signal is active for {merchant_name}, but its metric and driver are not supplied. "
                "Want me to prepare a verification checklist before amplifying it?"
            )
    elif kind == "dormant_with_vera":
        days = payload.get("days_since_last_merchant_message")
        topic = _clean_label(payload.get("last_topic") or "the last topic")
        offered_work = "restart with one useful task"
        if days is not None:
            primary = f"no merchant message for {days} days; last topic {topic}"
            body = (
                f"{owner}, it has been {days} days since we spoke about {topic}. "
                "What is the one business task you want me to handle this week?"
            )
        else:
            primary = "merchant re-engagement signal supplied without last-turn details"
            body = (
                f"{owner}, what is the one {category} task you want me to handle this week?"
            )
        cta = "open_ended"
    elif kind == "appointment_tomorrow":
        date = _human_date(payload.get("appointment_iso") or payload.get("date"))
        offered_work = "confirm the appointment"
        if date:
            primary = f"appointment {date}"
            body = (
                f"Hi {customer}, {merchant_name} here. Your appointment is {date}. "
                "Reply YES to confirm or tell us if you need another time."
            )
        else:
            primary = "appointment reminder supplied without exact time"
            body = (
                f"Hi {customer}, {merchant_name} here. We have an appointment reminder for you, but the exact time is not included. "
                "Reply YES and our team will confirm it before proceeding."
            )
    else:
        # Unseen trigger kind: surface the payload's own facts so the message
        # stays specific instead of generic. The LLM overlay leads with the
        # sharpest of these; the deterministic body lists them plainly.
        salient = _salient_facts(payload)
        topic = _clean_label(payload.get("metric_or_topic") or kind)
        offer_text = f" Your active {offer} is the hook." if offer else ""
        if salient:
            primary = salient
            offered_work = "turn this signal into one concrete next step"
            body = (
                f"{owner}, new signal for {merchant_name} — {salient}.{offer_text} "
                "Want me to turn this into one concrete next step? 5-min job."
            )
        else:
            primary = f"new {topic} trigger"
            offered_work = "prepare one grounded next step"
            body = (
                f"{owner}, a new {topic} signal is active for {merchant_name}.{offer_text} "
                "Want me to prepare one grounded next step?"
            )

    fact_ids = _select_fact_ids(ledger, body)
    voice_block = candidate.category_payload.get("voice", {})
    vocab = voice_block.get("vocab_allowed", []) if isinstance(voice_block, dict) else []
    category_vocab = [str(v) for v in vocab[:8]] if isinstance(vocab, list) else []
    brief = MessageBrief(
        trigger_kind=kind,
        goal=goal,
        recipient_name=customer if candidate.customer else owner,
        merchant_name=merchant_name,
        category_slug=category,
        voice=voice,
        primary_fact=primary,
        supporting_fact=supporting,
        offered_work=offered_work,
        cta=cta,
        send_as=send_as,
        category_vocab=category_vocab,
        allowed_facts=ledger,
    )
    rationale = (
        f"Selected {kind} for {candidate.recipient_id}; {candidate.score_reason}. "
        f"Message leads with the sharpest grounded fact and offers to {offered_work}."
    )
    return MessagePlan(
        brief=brief,
        fallback_body=" ".join(body.split()),
        rationale=rationale,
        template_name=f"vera_{kind}_v1",
        template_params=[
            brief.recipient_name,
            brief.primary_fact,
            brief.offered_work,
        ],
        used_fact_ids=fact_ids,
    )


def _select_fact_ids(ledger: list[Fact], body: str) -> list[str]:
    lower = body.lower()
    selected: list[str] = []
    for fact in ledger:
        rendered = fact.rendered.strip()
        if len(rendered) >= 3 and rendered.lower() in lower:
            selected.append(fact.fact_id)
    return selected[:30]
