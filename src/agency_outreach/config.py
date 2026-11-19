from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised for missing or invalid configuration."""


def _req(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required setting {name} (see .env.example)")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").split("#")[0].strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    sender_name: str
    sender_email: str
    phone: str
    city: str
    linkedin: str
    github: str
    resume_path: Path
    resume_url: str
    state_db: Path
    delay_min: int
    delay_max: int
    max_per_run: int
    auto_send: bool = False
    allow_multiple_per_recipient: bool = False
    master_resume: Path = Path("./data/master_resume.json")
    resume_dir: Path = Path("./resumes")
    tracker_path: Path = Path("./data/applications_tracker.xlsx")
    min_jd_chars: int = 40
    followup_after_days: int = 14
    skip_domains: tuple = ("gmail.com",)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_user: str = ""
    smtp_password: str = ""

    @classmethod
    def load(cls, env_file: str | None = ".env", require_smtp: bool = False) -> "Settings":
        if env_file:
            load_dotenv(env_file)
        smtp = {}
        if require_smtp:
            security = os.getenv("SMTP_SECURITY", "starttls").split("#")[0].strip().lower()
            if security not in {"starttls", "ssl"}:
                raise ConfigError("SMTP_SECURITY must be 'starttls' or 'ssl'")
            smtp = dict(
                smtp_host=_req("SMTP_HOST"),
                smtp_port=_int("SMTP_PORT", 587),
                smtp_security=security,
                smtp_user=_req("SMTP_USER"),
                smtp_password=_req("SMTP_PASSWORD"),
            )
        delay_min = _int("DELAY_MIN_SECONDS", 20)
        delay_max = _int("DELAY_MAX_SECONDS", 45)
        if delay_min < 0 or delay_max < delay_min:
            raise ConfigError("Require 0 <= DELAY_MIN_SECONDS <= DELAY_MAX_SECONDS")
        return cls(
            sender_name=_req("SENDER_NAME"),
            sender_email=_req("SENDER_EMAIL"),
            phone=_req("PHONE"),
            city=os.getenv("CITY", "Pune").strip(),
            linkedin=_req("LINKEDIN_URL"),
            github=_req("GITHUB_URL"),
            resume_path=Path(_req("RESUME_PATH")).expanduser(),
            resume_url=os.getenv("RESUME_URL", "").strip(),
            state_db=Path(os.getenv("STATE_DB", "./data/outreach_state.sqlite3")).expanduser(),
            delay_min=delay_min,
            delay_max=delay_max,
            max_per_run=_int("MAX_PER_RUN", 30),
            auto_send=_bool("AUTO_SEND_EMAILS", False),
            allow_multiple_per_recipient=_bool("ALLOW_MULTIPLE_JOBS_PER_RECIPIENT", False),
            master_resume=Path(os.getenv("MASTER_RESUME_JSON", "./data/master_resume.json")).expanduser(),
            resume_dir=Path(os.getenv("RESUME_OUTPUT_DIR", "./resumes")).expanduser(),
            tracker_path=Path(os.getenv("TRACKER_PATH", "./data/applications_tracker.xlsx")).expanduser(),
            min_jd_chars=_int("MIN_JD_CHARS", 40),
            followup_after_days=_int("FOLLOWUP_AFTER_DAYS", 14),
            skip_domains=tuple(d.strip().lower().lstrip("@") for d in
                               os.getenv("SKIP_EMAIL_DOMAINS", "gmail.com").split("#")[0].split(",") if d.strip()),
            **smtp,
        )
