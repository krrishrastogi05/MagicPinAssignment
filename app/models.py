from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Scope = Literal["category", "merchant", "customer", "trigger"]
SendAs = Literal["vera", "merchant_on_behalf"]
ReplyActionName = Literal["send", "wait", "end"]


class ContextPush(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Scope
    context_id: str = Field(min_length=1, max_length=300)
    version: int = Field(ge=0)
    payload: dict[str, Any]
    delivered_at: str

    @field_validator("delivered_at")
    @classmethod
    def valid_delivered_at(cls, value: str) -> str:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


class ContextAccepted(BaseModel):
    accepted: Literal[True] = True
    ack_id: str
    stored_at: str


class ContextRejected(BaseModel):
    accepted: Literal[False] = False
    reason: str
    current_version: int | None = None
    details: str | None = None


class TickRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    now: str
    available_triggers: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator("now")
    @classmethod
    def valid_now(cls, value: str) -> str:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


class TickAction(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: str | None = None
    send_as: SendAs
    trigger_id: str
    template_name: str
    template_params: list[str]
    body: str = Field(min_length=1)
    cta: str
    suppression_key: str
    rationale: str = Field(min_length=1)


class TickResponse(BaseModel):
    actions: list[TickAction]


class ReplyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conversation_id: str = Field(min_length=1, max_length=500)
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: Literal["merchant", "customer"]
    message: str = Field(min_length=1, max_length=20_000)
    received_at: str | None = None
    turn_number: int = Field(ge=1)

    @field_validator("received_at")
    @classmethod
    def valid_received_at(cls, value: str | None) -> str | None:
        if value is not None:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


class ReplyResponse(BaseModel):
    action: ReplyActionName
    body: str | None = None
    cta: str | None = None
    wait_seconds: int | None = Field(default=None, ge=1, le=604_800)
    rationale: str = Field(min_length=1)

    @field_validator("body")
    @classmethod
    def send_body_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("body cannot be blank")
        return value


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    uptime_seconds: int
    contexts_loaded: dict[str, int]


class MetadataResponse(BaseModel):
    team_name: str
    team_members: list[str]
    model: str
    approach: str
    contact_email: str
    version: str
    submitted_at: str


class Fact(BaseModel):
    fact_id: str
    value: Any
    rendered: str


class MessageBrief(BaseModel):
    trigger_kind: str
    goal: str
    recipient_name: str
    merchant_name: str
    category_slug: str
    voice: str
    primary_fact: str
    supporting_fact: str | None = None
    offered_work: str
    cta: str
    send_as: SendAs
    allowed_facts: list[Fact]


class LLMComposition(BaseModel):
    body: str = Field(min_length=1, max_length=1200)
    cta: str
    used_fact_ids: list[str]
    rationale_summary: str = Field(min_length=1, max_length=500)
