from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path = Path("data/vera.db")
    model_enabled: bool = True
    model_names: tuple[str, ...] = (
        "google:gemini-3.7-flash",
        "google:gemini-3.6-flash",
    )
    model_timeout_seconds: float = 18.0
    model_output_retries: int = 1
    evaluation_no_urls: bool = True
    strict_expiry: bool = False
    max_context_bytes: int = 500 * 1024
    max_actions_per_tick: int = 20
    team_name: str = "RisingStar"
    team_members: tuple[str, ...] = ("Krrish Rastogi",)
    contact_email: str = "krrishrastogi00@gmail.com"
    approach: str = (
        "deterministic policy + provenance-constrained Gemini composer + validated fallback"
    )
    version: str = "0.4.1"
    submitted_at: str = "not-submitted"

    @classmethod
    def from_env(cls) -> "Settings":
        models = tuple(
            part.strip()
            for part in os.getenv(
                "VERA_MODELS",
                "google:gemini-3.7-flash,google:gemini-3.6-flash",
            ).split(",")
            if part.strip()
        )
        members = tuple(
            part.strip()
            for part in os.getenv("VERA_TEAM_MEMBERS", "Krrish Rastogi").split(",")
            if part.strip()
        )
        return cls(
            database_path=Path(os.getenv("VERA_DATABASE_PATH", "data/vera.db")),
            model_enabled=_as_bool(os.getenv("VERA_MODEL_ENABLED"), True),
            model_names=models,
            model_timeout_seconds=max(
                15.0,
                min(
                    22.0,
                    float(os.getenv("VERA_MODEL_TIMEOUT_SECONDS", "18")),
                ),
            ),
            model_output_retries=int(os.getenv("VERA_MODEL_OUTPUT_RETRIES", "1")),
            evaluation_no_urls=_as_bool(
                os.getenv("VERA_EVALUATION_NO_URLS"), True
            ),
            strict_expiry=_as_bool(os.getenv("VERA_STRICT_EXPIRY"), False),
            max_context_bytes=int(
                os.getenv("VERA_MAX_CONTEXT_BYTES", str(500 * 1024))
            ),
            max_actions_per_tick=min(
                20, int(os.getenv("VERA_MAX_ACTIONS_PER_TICK", "20"))
            ),
            team_name=os.getenv("VERA_TEAM_NAME", "RisingStar"),
            team_members=members or ("Krrish Rastogi",),
            contact_email=os.getenv(
                "VERA_CONTACT_EMAIL", "krrishrastogi00@gmail.com"
            ),
            approach=os.getenv(
                "VERA_APPROACH",
                "deterministic policy + provenance-constrained Gemini composer + validated fallback",
            ),
            version=os.getenv("VERA_VERSION", "0.4.1"),
            submitted_at=os.getenv("VERA_SUBMITTED_AT", "not-submitted"),
        )
