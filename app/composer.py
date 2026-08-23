from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from app.config import Settings
from app.models import Fact, LLMComposition, MessageBrief, TickAction
from app.policy import Candidate
from app.storage import Store, stable_hash


URL_RE = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
NUMBER_RE = re.compile(r"(?<![\w])[-+]?\d[\d,]*(?:\.\d+)?%?")


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
        primary = f"{title} — {source}"
        offered_work = "turn the finding into a customer-ready message"
        ctr = candidate.merchant_payload.get("performance", {}).get("ctr")
        peer_ctr = candidate.category_payload.get("peer_stats", {}).get("avg_ctr")
        metric_hook = ""
        if isinstance(ctr, (int, float)) and isinstance(peer_ctr, (int, float)):
            metric_hook = f" Your CTR is {_pct(ctr)} vs {_pct(peer_ctr)} for the supplied peer benchmark."
        offer_hook = f" {offer} is already active." if offer else ""
        body = (
            f"{owner}, {title}. Source: {source}.{metric_hook}{offer_hook} "
            f"Want me to turn the useful takeaway into a customer-ready WhatsApp?"
        )
    elif kind == "regulation_change":
        title = str((digest or {}).get("title") or "A regulation update was supplied")
        source = str((digest or {}).get("source") or "the supplied regulatory update")
        deadline = _human_date(payload.get("deadline_iso"))
        primary = f"{title}; deadline {deadline}"
        offered_work = "prepare a compliance checklist"
        body = (
            f"{owner}, {title}. Deadline: {deadline}. Source: {source}. "
            "Want me to prepare a precise compliance checklist?"
        )
    elif kind == "recall_due":
        service = _clean_label(payload.get("service_due") or "service recall")
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
                f"{offer_text} Available: {slot_text}. Reply 1 or 2 to reserve."
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
                "Want me to draft one focused recovery message?"
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
        trial = _human_date(payload.get("trial_completed"))
        window = _clean_label(payload.get("next_step_window_open") or "next preparation window")
        offered_work = "reserve the next preparation step"
        if wedding and trial:
            primary = f"wedding {wedding}; trial completed {trial}"
            body = (
                f"Hi {customer}, {merchant_name} here. Your bridal trial was {trial} and the wedding is {wedding}; "
                f"the {window} is open. Want us to reserve the next step?"
            )
        else:
            primary = "wedding follow-up supplied without event dates"
            body = (
                f"Hi {customer}, {merchant_name} here. Your wedding-package follow-up is due, but the event dates are not included. "
                "Reply YES and our team will confirm the next preparation step."
            )
    elif kind == "curious_ask_due":
        primary = _clean_label(payload.get("ask_template") or "weekly demand check")
        offered_work = "turn the answer into one concrete offer"
        locality = _locality(candidate)
        location_hook = f" in {locality}" if locality else ""
        offer_hook = f" You already have {offer} active." if offer else ""
        body = (
            f"{owner}, which {_category_item(category)} are customers asking for most{location_hook} this week?"
            f"{offer_hook} Reply with its name and I'll turn the answer into one concrete message."
        )
        cta = "open_ended"
    elif kind == "winback_eligible":
        days = payload.get("days_since_expiry")
        dip = _pct(payload.get("perf_dip_pct"))
        lapsed = payload.get("lapsed_customers_added_since_expiry")
        primary = f"expired {days} days; performance {dip}; {lapsed} new lapsed customers"
        offered_work = "draft a low-risk win-back plan"
        body = (
            f"{owner}, it has been {days} days since expiry; performance is {dip} and {lapsed} customers were added to the lapsed pool. "
            "Want me to draft a low-risk win-back plan?"
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
                f"{owner}, {match} is at {venue} at {time}. {source} shows Saturday IPL covers down 12% versus the Saturday average. "
                f"Want me to adapt {offer or 'your active offer'} for delivery instead?"
            )
        else:
            body = (
                f"{owner}, {match} is at {venue} at {time}. "
                f"Want me to draft a timely push using {offer or 'your current menu'}?"
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
                f"{owner}, {theme} appeared in {occurrences} reviews this month.{quote_text} "
                "Want me to draft one public response and one operational fix?"
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
            structure = "lead with the menu, minimum headcount, delivery window, then quote only after quantity is confirmed"
        elif category == "gyms":
            structure = "define the participant level, batch schedule, coach, then add the fee only after it is confirmed"
        elif category == "salons":
            structure = "define the service sequence, preparation window, and confirmed package price"
        else:
            structure = "state the outcome, the confirmed inclusions, and the next decision"
        body = (
            f"{owner}, first usable structure for {topic} at {merchant_name}: {structure}. "
            "Reply CONFIRM and I'll turn this into the customer-facing copy."
        )
        supporting = last or supporting
        cta = "binary_confirm_cancel"
    elif kind == "seasonal_perf_dip":
        metric = _clean_label(payload.get("metric") or "performance")
        delta = _pct(payload.get("delta_pct"))
        season = _clean_label(payload.get("season_note") or "seasonal window")
        primary = f"{metric} is {delta}; expected {season}"
        offered_work = "reframe the seasonal dip into an acquisition message"
        offer_text = f" around {offer}" if offer else ""
        body = (
            f"{owner}, {metric} changed {delta}, but this matches the {season}. "
            f"Want me to turn the dip into a focused acquisition message{offer_text}?"
        )
    elif kind in {"customer_lapsed_soft", "customer_lapsed_hard"}:
        days = payload.get("days_since_last_visit")
        focus = _clean_label(payload.get("previous_focus") or "previous service")
        offered_work = "reserve an easy return step"
        offer_text = f" {offer}." if offer else ""
        if days is not None:
            primary = f"last visit {days} days ago; previous focus {focus}"
            body = (
                f"Hi {customer}, {merchant_name} here. It has been {days} days since your last visit; your previous focus was {focus}."
                f"{offer_text} Want us to reserve an easy return slot?"
            )
        else:
            last_visit = _human_date(
                (candidate.customer_payload or {}).get("relationship", {}).get("last_visit")
            )
            primary = f"customer state {kind}; last visit {last_visit}"
            when = f" since your {last_visit} visit" if last_visit else ""
            body = (
                f"Hi {customer}, {merchant_name} here. We wanted to reconnect{when}."
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
                f"Hi {customer}, {merchant_name} here. Following up on the {trial} trial: {', '.join(labels)} is available. "
                "Reply 1 to reserve it."
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
        primary = f"{molecule}; batches {batches}; {manufacturer}"
        offered_work = "filter affected stock and prepare customer outreach"
        body = (
            f"{owner}, supply alert for {molecule}: {manufacturer} batches {batches}. Source: {source}. "
            "Want me to prepare an affected-stock checklist?"
        )
    elif kind == "chronic_refill_due":
        molecules = ", ".join(_clean_label(item) for item in payload.get("molecule_list", []))
        runs_out = _human_date(payload.get("stock_runs_out_iso"))
        delivery = bool(payload.get("delivery_address_saved"))
        offered_work = "confirm the refill delivery"
        delivery_text = " Your delivery address is saved." if delivery else ""
        if molecules and runs_out:
            primary = f"{molecules} run out {runs_out}"
            body = (
                f"Hi {customer}, {merchant_name} here. Your {molecules} refill is expected to run out {runs_out}.{delivery_text} "
                "Reply YES to confirm the refill request."
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
            f"{owner}, your{location_hook} profile is still unverified; the supplied estimate is {uplift} uplift after verification. "
            f"The available path is {path}. Want me to walk you through it?"
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
                f"Want me to draft a differentiated response using {offer or 'your real strengths'}?"
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
                f"Want me to draft the next message{offer_hook} while the signal is fresh?"
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
        topic = _clean_label(payload.get("metric_or_topic") or kind)
        primary = f"new {topic} trigger"
        offered_work = "prepare one grounded next step"
        offer_text = f" using {offer}" if offer else ""
        body = (
            f"{owner}, a new {topic} signal is active for {merchant_name}. "
            f"Want me to prepare one grounded next step{offer_text}?"
        )

    fact_ids = _select_fact_ids(ledger, body)
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
        allowed_facts=ledger,
    )
    rationale = (
        f"Selected {kind} for {candidate.recipient_id}; {candidate.score_reason}. "
        f"Message uses the current trigger plus merchant/customer context and offers {offered_work}."
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
    PROMPT_VERSION = "composer_v1"
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
                    "You compose one concise WhatsApp message for Vera. The decision is already made. "
                    "Use only allowed facts, preserve the requested CTA, never invent numbers/names/offers/sources, "
                    "and return the fact_ids you actually used. Ask at most one question."
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
                errors = validate_composition(
                    body=output.body,
                    cta=output.cta,
                    used_fact_ids=output.used_fact_ids,
                    ledger=ledger,
                    category=candidate.category_payload,
                    expected_cta=plan.brief.cta,
                    previous_body=previous_body,
                    no_urls=self.settings.evaluation_no_urls,
                )
                if not errors:
                    body = " ".join(output.body.split())
                    cta = output.cta
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
