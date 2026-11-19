from __future__ import annotations

import logging
import mimetypes
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from .config import Settings
from .models import Agency
from .templating import render

log = logging.getLogger(__name__)
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
AUTH_HELP = (
    "SMTP authentication failed. For Gmail you must use an APP PASSWORD, not your account password: "
    "turn on 2-Step Verification, then Google Account > Security > App passwords, create one, "
    "and put it in SMTP_PASSWORD. SMTP_USER must be the same Gmail address."
)


class SendError(RuntimeError):
    """Permanent failure for one recipient (do not retry)."""


def build_message(agency: Agency, cfg: Settings, template_dir: Path) -> EmailMessage:
    """Build one individual message (never CC/BCC other agencies)."""
    if not agency.email:
        raise SendError(f"{agency.name}: no email address")
    resume = cfg.resume_path
    if not resume.is_file():
        raise SendError(f"Resume not found: {resume}")
    if resume.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise SendError(f"Resume larger than {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB")

    msg = EmailMessage()
    msg["Subject"] = render(template_dir / "email_subject.txt", agency, cfg)
    msg["From"] = formataddr((cfg.sender_name, cfg.sender_email))
    msg["To"] = agency.email
    msg["Reply-To"] = cfg.sender_email
    msg["Message-ID"] = make_msgid(domain=cfg.sender_email.split("@")[-1])
    msg.set_content(render(template_dir / "email_body.txt", agency, cfg))

    ctype = mimetypes.guess_type(resume.name)[0] or "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)
    msg.add_attachment(resume.read_bytes(), maintype=maintype, subtype=subtype,
                       filename=resume.name)
    return msg


def build_application_message(to: str, subject: str, body: str, resume: Path, cfg: Settings) -> EmailMessage:
    """One individual job-application message with the job-specific resume attached."""
    if not to:
        raise SendError("no recipient address")
    if not resume.is_file():
        raise SendError(f"Resume not found: {resume}")
    if resume.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise SendError(f"Resume larger than {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.sender_name, cfg.sender_email))
    msg["To"] = to
    msg["Reply-To"] = cfg.sender_email
    msg["Message-ID"] = make_msgid(domain=cfg.sender_email.split("@")[-1])
    msg.set_content(body)
    msg.add_attachment(resume.read_bytes(), maintype="application", subtype="pdf", filename=resume.name)
    return msg


class SmtpMailer:
    """Context-managed SMTP connection with reconnect + bounded retries."""

    def __init__(self, cfg: Settings, retries: int = 3, backoff: float = 5.0):
        self.cfg, self.retries, self.backoff = cfg, retries, backoff
        self._conn: smtplib.SMTP | None = None

    def __enter__(self) -> "SmtpMailer":
        self._connect()
        return self

    def __exit__(self, *exc) -> None:
        if self._conn:
            try:
                self._conn.quit()
            except smtplib.SMTPException:
                pass
            self._conn = None

    def _connect(self) -> None:
        c, ctx = self.cfg, ssl.create_default_context()
        # Google shows app passwords in groups of four with spaces; SMTP wants them without.
        password = c.smtp_password.replace(" ", "") if c.smtp_host.endswith("gmail.com") else c.smtp_password
        try:
            if c.smtp_security == "ssl":
                conn: smtplib.SMTP = smtplib.SMTP_SSL(c.smtp_host, c.smtp_port, context=ctx, timeout=30)
            else:
                conn = smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=30)
                conn.starttls(context=ctx)
            conn.login(c.smtp_user, password)
        except smtplib.SMTPAuthenticationError as exc:
            raise SendError(AUTH_HELP) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise SendError(f"Could not connect to {c.smtp_host}:{c.smtp_port} - {exc}") from exc
        self._conn = conn
        log.info("SMTP connected to %s:%s", c.smtp_host, c.smtp_port)

    def send(self, msg: EmailMessage) -> None:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                if self._conn is None:
                    self._connect()
                assert self._conn is not None
                self._conn.send_message(msg)
                return
            except smtplib.SMTPRecipientsRefused as exc:
                raise SendError(f"Recipient refused: {exc.recipients}") from exc
            except smtplib.SMTPAuthenticationError as exc:
                raise SendError(AUTH_HELP) from exc
            except smtplib.SMTPResponseException as exc:
                if 500 <= exc.smtp_code < 600:
                    raise SendError(f"SMTP {exc.smtp_code}: {exc.smtp_error!r}") from exc
                last = exc
            except (smtplib.SMTPException, OSError) as exc:
                last = exc
            log.warning("Send attempt %d/%d failed: %s", attempt, self.retries, last)
            self._conn = None  # force reconnect
            time.sleep(self.backoff * attempt)
        raise SendError(f"Gave up after {self.retries} attempts: {last}")
