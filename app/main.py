from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.composer import Composer, _active_offer, _merchant_name
from app.config import Settings
from app.models import (
    ContextAccepted,
    ContextPush,
    ContextRejected,
    HealthResponse,
    MetadataResponse,
    ReplyRequest,
    ReplyResponse,
    TickRequest,
    TickResponse,
)
from app.policy import DecisionEngine
from app.reply import ReplyEngine
from app.storage import Store, canonical_json, stable_hash, utc_now


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.database_path)
    store.initialize()
    policy = DecisionEngine(store, settings)
    composer = Composer(store, settings)
    reply_engine = ReplyEngine(store)
    started = time.monotonic()

    app = FastAPI(
        title="Vera Grounded Bot",
        version=settings.version,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.policy = policy
    app.state.composer = composer
    app.state.reply_engine = reply_engine

    @app.middleware("http")
    async def reject_oversized_context(request: Request, call_next: Any):
        if request.url.path == "/v1/context":
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > settings.max_context_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "accepted": False,
                        "reason": "payload_too_large",
                        "details": f"Maximum context envelope is {settings.max_context_bytes} bytes.",
                    },
                )
        return await call_next(request)

    @app.post(
        "/v1/context",
        response_model=ContextAccepted,
        responses={409: {"model": ContextRejected}, 413: {"model": ContextRejected}},
    )
    async def push_context(push: ContextPush):
        size = len(canonical_json(push.model_dump(mode="json")).encode("utf-8"))
        if size > settings.max_context_bytes:
            return JSONResponse(
                status_code=413,
                content=ContextRejected(
                    reason="payload_too_large",
                    details=f"Context envelope is {size} bytes; limit is {settings.max_context_bytes}.",
                ).model_dump(mode="json"),
            )

        accepted, current = store.put_context(
            scope=push.scope,
            context_id=push.context_id,
            version=push.version,
            payload=push.payload,
            delivered_at=push.delivered_at,
        )
        if not accepted:
            return JSONResponse(
                status_code=409,
                content=ContextRejected(
                    reason="stale_version",
                    current_version=current.version if current else None,
                    details="Only a strictly higher version may replace stored context.",
                ).model_dump(mode="json"),
            )
        ack_id = f"ack_{stable_hash([push.scope, push.context_id, push.version])[:20]}"
        return ContextAccepted(ack_id=ack_id, stored_at=utc_now())

    @app.post("/v1/tick", response_model=TickResponse)
    async def tick(request: TickRequest) -> TickResponse:
        candidates = []
        for trigger_id in dict.fromkeys(request.available_triggers):
            trigger = store.get_context("trigger", trigger_id)
            if trigger is None:
                continue
            candidate = policy.build_candidate(trigger, request.now)
            if candidate is not None:
                candidates.append(candidate)

        reserved_candidates = []
        for candidate in policy.select(candidates):
            reserved = store.reserve_suppression(
                suppression_key=candidate.suppression_key,
                recipient_id=candidate.recipient_id,
                trigger_id=candidate.trigger.context_id,
                expires_at=candidate.trigger_payload.get("expires_at"),
            )
            if not reserved:
                continue

            conversation_id = "conv_" + stable_hash(
                {
                    "recipient": candidate.recipient_id,
                    "trigger": candidate.trigger.context_id,
                    "suppression": candidate.suppression_key,
                }
            )[:24]
            reserved_candidates.append((candidate, conversation_id))

        # A tick may contain several independent recipients. Compose them in
        # parallel so one slow model call cannot multiply the endpoint latency.
        # Every composition still has its own strict timeout and safe fallback.
        compositions = await asyncio.gather(
            *(
                composer.compose(candidate, conversation_id)
                for candidate, conversation_id in reserved_candidates
            ),
            return_exceptions=True,
        )

        actions = []
        for (candidate, conversation_id), composition in zip(
            reserved_candidates, compositions, strict=True
        ):
            if isinstance(composition, BaseException):
                store.release_suppression(
                    candidate.suppression_key, candidate.recipient_id
                )
                continue
            try:
                action = composition
                context = {
                    "goal": action.template_name.removeprefix("vera_").removesuffix("_v1").replace("_", " "),
                    "merchant_name": _merchant_name(candidate),
                    "active_offer": _active_offer(candidate),
                    "suppression_key": candidate.suppression_key,
                    "last_rationale": action.rationale,
                }
                store.create_conversation(
                    conversation_id=conversation_id,
                    merchant_id=action.merchant_id,
                    customer_id=action.customer_id,
                    trigger_id=action.trigger_id,
                    state="sent",
                    last_body=action.body,
                    context=context,
                )
                store.mark_suppression_sent(
                    candidate.suppression_key,
                    candidate.recipient_id,
                    conversation_id,
                )
                actions.append(action)
            except Exception:
                # One malformed trigger must not fail the whole tick or poison dedup.
                store.release_suppression(
                    candidate.suppression_key, candidate.recipient_id
                )

        return TickResponse(actions=actions)

    @app.post("/v1/reply", response_model=ReplyResponse)
    async def reply(request: ReplyRequest) -> ReplyResponse:
        return reply_engine.handle(request)

    @app.get("/v1/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(
            uptime_seconds=max(0, int(time.monotonic() - started)),
            contexts_loaded=store.context_counts(),
        )

    @app.get("/v1/metadata", response_model=MetadataResponse)
    async def metadata() -> MetadataResponse:
        return MetadataResponse(
            team_name=settings.team_name,
            team_members=list(settings.team_members),
            model=composer.model_label,
            approach=settings.approach,
            contact_email=settings.contact_email,
            version=settings.version,
            submitted_at=settings.submitted_at,
        )

    return app


app = create_app()
