"""LinkedIn lead -> tailored resume -> personalised draft -> approval -> send, with Excel + SQLite tracking."""
from __future__ import annotations

import hashlib
import logging
import random
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from .config import Settings
from .drafts import build_body, build_followup, build_subject
from .email_sender import SendError, SmtpMailer, build_application_message
from .job_analysis import NOT_AVAILABLE, analyze_job, extract_emails, pick_recruiter_email
from .models import Lead
from .resume_builder import ResumeError, build_resume, load_master, pdf_job_id
from .state import StateStore
from .tracker import Tracker, TrackerError

log = logging.getLogger("agency_outreach.applications")

# statuses
PENDING, GENERATED, AWAITING, APPROVED, REJECTED = "Pending", "Resume Generated", "Awaiting Approval", "Approved", "Rejected"
SENDING, SENT, SKIPPED, FAILED = "Sending", "Sent", "Skipped", "Failed"


def domain_excluded(email: str, cfg: Settings) -> str:
    """The configured excluded domain this address belongs to ('' if none). Subdomains count."""
    domain = (email or "").rpartition("@")[2].strip().lower()
    for d in cfg.skip_domains:
        if domain == d or domain.endswith("." + d):
            return d
    return ""


def mask(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "n/a"


def fingerprint(job_id: str, email: str, resume_path: str) -> str:
    return hashlib.sha256(f"{job_id}|{email.lower()}|{Path(resume_path).name}".encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ prepare
def prepare(leads: Iterable[Lead], tracker: Tracker, store: StateStore, cfg: Settings,
            limit: int | None = None) -> Counter:
    """Seed/update tracker rows, generate tailored resumes and e-mail drafts. Safe to re-run."""
    stats: Counter = Counter()
    master = load_master(cfg.master_resume)
    if not cfg.resume_path.is_file():
        raise ResumeError(f"Original resume not found: {cfg.resume_path}")  # master copy must exist
    seen: set[str] = set()
    done = 0
    try:
        for lead in leads:
            analysis = analyze_job(lead.post_url, lead.job_title, lead.company, lead.post_text, cfg.min_jd_chars)
            jid = analysis.job_id
            if jid in seen:
                stats["duplicate_in_input"] += 1
                continue
            seen.add(jid)
            row = tracker.get(jid)
            if row and row.get("Resume Generation Status") == GENERATED and row.get("Customized Resume Path") \
                    and Path(row["Customized Resume Path"]).is_file():
                stats["already_prepared"] += 1
                continue
            if limit is not None and done >= limit:
                stats["deferred_by_limit"] += 1
                continue
            done += 1
            try:
                _prepare_one(lead, analysis, tracker, store, cfg, master, stats)
            except Exception as exc:  # one bad record must not stop the batch
                log.exception("prepare failed for %s", jid)
                tracker.set(jid, {"LinkedIn Post URL": lead.post_url, "Company Name": lead.company or NOT_AVAILABLE,
                                  "Job Title": lead.job_title, "Resume Generation Status": FAILED,
                                  "Error Details": f"{type(exc).__name__}: {exc}"[:500]})
                stats["failed"] += 1
            tracker.save()
    finally:
        tracker.save()
    return stats


def _prepare_one(lead, analysis, tracker, store, cfg, master, stats) -> None:
    jid = analysis.job_id
    email, source = pick_recruiter_email(lead.post_text, lead.email or "")
    base = {
        "LinkedIn Post URL": lead.post_url, "Company Name": lead.company or NOT_AVAILABLE,
        "Job Title": lead.job_title, "Job Description": lead.post_text,
        "Required Skills": ", ".join(analysis.skill_names) or NOT_AVAILABLE,
        "Experience Required": analysis.experience, "Job Location": lead.location or NOT_AVAILABLE,
        "Application Instructions": analysis.instructions,
        "Recruiter Name": lead.name or NOT_AVAILABLE, "Recruiter Email": email, "Error Details": "",
    }
    existing = tracker.get(jid)
    if not analysis.complete:
        tracker.set(jid, {**base, "Resume Generation Status": SKIPPED, "Email Approval Status": SKIPPED,
                          "Email Sending Status": SKIPPED, "Error Details": analysis.problem})
        stats["skipped_incomplete_jd"] += 1
        return
    try:
        built = build_resume(master, analysis, lead.job_title, lead.company, cfg.resume_dir)
    except ResumeError as exc:
        tracker.set(jid, {**base, "Resume Generation Status": FAILED, "Error Details": str(exc)})
        stats["resume_failed"] += 1
        return
    fields = {**base, "Customized Resume Path": str(built.path), "Resume Generation Status": GENERATED}
    if email == NOT_AVAILABLE:
        fields.update({"Email Approval Status": SKIPPED, "Email Sending Status": SKIPPED,
                       "Error Details": "Recruiter email not available - email step skipped"})
        stats["no_email"] += 1
    else:
        subject = build_subject(lead.job_title, lead.company, cfg)
        body = build_body(recruiter_name=lead.name, job_title=lead.job_title, company=lead.company,
                          location=lead.location, analysis=analysis, master=master, cfg=cfg)
        fp = fingerprint(jid, email, str(built.path))
        prior_sent = existing.get("Email Sending Status") == SENT
        fields.update({"Email Subject": subject, "Email Body or Template": body, "Draft Fingerprint": fp})
        if not prior_sent:
            fields["Email Approval Status"] = APPROVED if cfg.auto_send else AWAITING
            fields["Email Sending Status"] = PENDING
            store.app_upsert(jid, email, lead.post_url, str(built.path), fp, "prepared")
        stats["drafts_ready"] += 1
    tracker.set(jid, fields)
    stats["resumes_generated"] += 1


# ------------------------------------------------------------------ duplicates / approve / review
_OPEN = (AWAITING, APPROVED)


def skip_duplicates(tracker: Tracker, store: StateStore, cfg: Settings) -> int:
    """Mark not-yet-sent rows that must never be e-mailed as Skipped, so they do not clutter review
    or use up the per-run send cap: (a) address already e-mailed before, (b) job already sent,
    (c) same recipient appears earlier in this open batch (first one wins)."""
    n, batch_seen = 0, {}
    for jid in tracker.job_ids():
        r = tracker.get(jid)
        if r.get("Email Approval Status") not in _OPEN or (r.get("Email Sending Status") or PENDING) != PENDING:
            continue
        email = (r.get("Recruiter Email") or "").strip().lower()
        reason = ""
        app_row = store.app_get(jid)
        if domain_excluded(email, cfg):
            reason = f"Skipped: recipient domain excluded ({domain_excluded(email, cfg)})"
        elif app_row and app_row["status"] == "sent":
            reason = "Duplicate: an application for this job was already sent"
        elif not cfg.allow_multiple_per_recipient and email and store.already_sent(email):
            reason = "Duplicate: an e-mail was already sent to this recipient"
        elif not cfg.allow_multiple_per_recipient and email in batch_seen:
            reason = f"Duplicate: same recipient as job {batch_seen[email]} in this batch"
        if reason:
            tracker.set(jid, {"Email Approval Status": SKIPPED, "Email Sending Status": SKIPPED, "Error Details": reason})
            n += 1
        elif email:
            batch_seen[email] = jid
    if n:
        tracker.save()
    return n


def pending_review(tracker: Tracker, store: StateStore | None = None, cfg: Settings | None = None) -> list[dict[str, str]]:
    if store is not None and cfg is not None:
        skip_duplicates(tracker, store, cfg)
    return [r for r in (tracker.get(j) for j in tracker.job_ids())
            if r.get("Email Approval Status") == AWAITING]


def approve(tracker: Tracker, job_ids: list[str] | None, approve_all: bool,
            store: StateStore | None = None, cfg: Settings | None = None) -> int:
    if store is not None and cfg is not None:
        skip_duplicates(tracker, store, cfg)
    n = 0
    for jid in tracker.job_ids():
        r = tracker.get(jid)
        if r.get("Email Approval Status") != AWAITING:
            continue
        if approve_all or (job_ids and jid in job_ids):
            tracker.set(jid, {"Email Approval Status": APPROVED})
            n += 1
    tracker.save()
    return n


# ------------------------------------------------------------------ send
MailerFactory = Callable[[Settings], object]


def send(tracker: Tracker, store: StateStore, cfg: Settings, *, really_send: bool,
         limit: int | None = None, retry_failed: bool = False, send_all: bool = False,
         mailer_factory: MailerFactory = SmtpMailer,
         sleep: Callable[[float], None] = time.sleep) -> Counter:
    stats: Counter = Counter()
    _recover_interrupted(tracker, store, stats)
    stats["duplicates_skipped"] += skip_duplicates(tracker, store, cfg)
    if not stats["duplicates_skipped"]:
        del stats["duplicates_skipped"]
    eligible = []
    for jid in tracker.job_ids():
        r = tracker.get(jid)
        if r.get("Email Approval Status") != APPROVED:
            continue
        st = r.get("Email Sending Status", PENDING) or PENDING
        if st == PENDING or (retry_failed and st == FAILED):
            eligible.append(jid)
    cap = 10**9 if send_all else min(limit or cfg.max_per_run, cfg.max_per_run)   # cap = e-mails actually sent/planned this run
    ready = 0

    mailer_cm = mailer_factory(cfg) if really_send and eligible else None
    mailer = None
    try:
        if mailer_cm is not None:
            mailer = mailer_cm.__enter__()
        for i, jid in enumerate(eligible, 1):
            if ready >= cap:
                stats["left_for_next_run"] += 1
                continue
            r = tracker.get(jid)
            email = r.get("Recruiter Email", "")
            problem = _validate_before_send(jid, r, store, cfg)
            if problem:
                kind, detail = problem
                tracker.set(jid, {"Email Sending Status": kind, "Error Details": detail})
                stats[kind.lower()] += 1
                log.warning("%s %s: %s", kind, jid, detail)
                tracker.save()
                continue
            ready += 1
            if not really_send:
                stats["would_send"] += 1
                print(f"  would send -> {email}  [{r['Job Title']} @ {r['Company Name']}]  attach {r['Customized Resume Path']}")
                continue
            try:
                msg = build_application_message(email, r["Email Subject"], r["Email Body or Template"],
                                                Path(r["Customized Resume Path"]), cfg)
            except SendError as exc:
                tracker.set(jid, {"Email Sending Status": FAILED, "Error Details": str(exc)[:500]})
                stats["failed"] += 1
                tracker.save()
                continue
            # mark BEFORE the network call: a crash in between is detected on restart, never re-sent blindly
            store.app_set_status(jid, "sending")
            tracker.set(jid, {"Email Sending Status": SENDING, "Error Details": ""})
            tracker.save()
            try:
                mailer.send(msg)  # type: ignore[union-attr]
            except SendError as exc:
                store.app_set_status(jid, "failed", str(exc))
                store.record(email, r["Company Name"], "failed", str(exc))
                tracker.set(jid, {"Email Sending Status": FAILED, "Error Details": str(exc)[:500]})
                stats["failed"] += 1
                tracker.save()
                log.error("FAILED %s -> %s: %s", jid, mask(email), exc)
                if "authentication" in str(exc).lower():
                    break
                continue
            store.app_set_status(jid, "sent")
            store.record(email, r["Company Name"] or jid, "sent")
            tracker.set(jid, {"Email Sending Status": SENT, "Email Sent Timestamp": _now(), "Error Details": ""})
            tracker.save()
            stats["sent"] += 1
            log.info("[%d/%d] sent %s -> %s", i, len(eligible), jid, mask(email))
            if ready < cap and i < len(eligible):
                sleep(random.uniform(cfg.delay_min, cfg.delay_max))
    finally:
        if mailer_cm is not None:
            mailer_cm.__exit__(None, None, None)
        tracker.save()
    return stats


def _recover_interrupted(tracker: Tracker, store: StateStore, stats: Counter) -> None:
    """A row stuck in 'Sending' means the process died mid-send: outcome unknown, so never auto-resend."""
    for jid in tracker.job_ids():
        r = tracker.get(jid)
        if r.get("Email Sending Status") == SENDING:
            app = store.app_get(jid)
            if app and app["status"] == "sent":
                tracker.set(jid, {"Email Sending Status": SENT, "Error Details": ""})
            else:
                tracker.set(jid, {"Email Sending Status": FAILED,
                                  "Error Details": "Interrupted while sending - outcome unknown. Check your Sent folder; "
                                                   "set status to Pending to retry or Sent if it went out."})
                store.app_set_status(jid, "failed", "interrupted while sending")
            stats["recovered_interrupted"] += 1
    tracker.save()


def _validate_before_send(jid: str, r: dict[str, str], store: StateStore, cfg: Settings) -> tuple[str, str] | None:
    """Return (status, detail) if the record must NOT be sent, else None."""
    email = (r.get("Recruiter Email") or "").strip().lower()
    if not email or email == NOT_AVAILABLE.lower():
        return SKIPPED, "No recruiter email"
    if extract_emails(email) != [email]:
        return FAILED, "Invalid recruiter email address"
    if domain_excluded(email, cfg):
        return SKIPPED, f"Skipped: recipient domain excluded ({domain_excluded(email, cfg)})"
    if not (r.get("Email Subject") and r.get("Email Body or Template")):
        return FAILED, "Email draft missing"
    resume = Path(r.get("Customized Resume Path") or "")
    if not resume.is_file():
        return FAILED, f"Customized resume file missing: {resume}"
    app = store.app_get(jid)
    if not app or app["email"] != email:
        return FAILED, "Recipient differs from the one the draft was prepared for; clear 'Resume Generation Status' and re-run prepare"
    if r.get("Draft Fingerprint") != fingerprint(jid, email, str(resume)) or app["fingerprint"] != r.get("Draft Fingerprint"):
        return FAILED, "Draft fingerprint mismatch (job, recipient or resume changed); re-run prepare"
    if pdf_job_id(resume) != jid:
        return FAILED, "Resume file was not generated for this job post"
    if app["status"] == "sent":
        return SKIPPED, "Duplicate: an application for this job was already sent"
    if not cfg.allow_multiple_per_recipient and store.already_sent(email):
        return SKIPPED, "Duplicate: an e-mail was already sent to this recipient"
    return None


# ------------------------------------------------------------------ follow-ups
_REPLIED = {"yes", "y", "true", "replied", "1"}


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat((value or "").strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def followup(tracker: Tracker, store: StateStore, cfg: Settings, *, days: int, really_send: bool,
             limit: int | None = None, send_all: bool = False, mailer_factory: MailerFactory = SmtpMailer,
             sleep: Callable[[float], None] = time.sleep) -> Counter:
    """One short follow-up per recruiter whose application was sent >= `days` days ago.

    Never to: recipients marked Replied=Yes in the tracker, excluded domains (gmail.com...), anyone already
    followed up (or whose follow-up outcome is unknown), or the same address twice in one run.
    """
    stats: Counter = Counter()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    candidates: list[str] = []
    seen: set[str] = set()
    for jid in tracker.job_ids():
        r = tracker.get(jid)
        if r.get("Email Sending Status") != SENT:
            continue
        ts = _parse_ts(r.get("Email Sent Timestamp", ""))
        if ts is None:
            continue
        if ts > cutoff:
            stats["too_recent"] += 1
            continue
        email = (r.get("Recruiter Email") or "").strip().lower()
        if r.get("Follow-up Status") in (SENT, SENDING):
            continue
        if (r.get("Replied") or "").strip().lower() in _REPLIED:
            stats["replied_excluded"] += 1
            continue
        if domain_excluded(email, cfg):
            stats["excluded_domain"] += 1
            continue
        if email in seen or store.followup_done(email):
            stats["already_followed_up"] += 1
            continue
        seen.add(email)
        candidates.append(jid)

    cap = 10**9 if send_all else min(limit or cfg.max_per_run, cfg.max_per_run)
    mailer_cm = mailer_factory(cfg) if really_send and candidates else None
    mailer, planned = None, 0
    try:
        if mailer_cm is not None:
            mailer = mailer_cm.__enter__()
        for jid in candidates:
            if planned >= cap:
                stats["left_for_next_run"] += 1
                continue
            r = tracker.get(jid)
            email = r["Recruiter Email"].strip().lower()
            resume = Path(r.get("Customized Resume Path") or "")
            if extract_emails(email) != [email] or not resume.is_file() or pdf_job_id(resume) != jid:
                tracker.set(jid, {"Follow-up Status": FAILED, "Error Details": "Follow-up blocked: invalid recipient or resume does not match this job"})
                stats["blocked"] += 1
                tracker.save()
                continue
            planned += 1
            subject, body = build_followup(r, cfg)
            if not really_send:
                stats["would_send"] += 1
                print(f"  would follow up -> {email}  [{r['Job Title']} @ {r['Company Name']}]  originally sent {r['Email Sent Timestamp'][:10]}")
                continue
            try:
                msg = build_application_message(email, subject, body, resume, cfg)
            except SendError as exc:
                tracker.set(jid, {"Follow-up Status": FAILED, "Error Details": str(exc)[:500]})
                stats["failed"] += 1
                tracker.save()
                continue
            store.followup_record(email, jid, "sending")      # marked first: a crash never causes a blind re-send
            tracker.set(jid, {"Follow-up Status": SENDING})
            tracker.save()
            try:
                mailer.send(msg)  # type: ignore[union-attr]
            except SendError as exc:
                permanent = "refused" in str(exc).lower() or "SMTP 5" in str(exc)
                store.followup_record(email, jid, "rejected" if permanent else "failed", str(exc))
                tracker.set(jid, {"Follow-up Status": FAILED, "Error Details": str(exc)[:500]})
                stats["failed"] += 1
                tracker.save()
                log.error("follow-up FAILED %s -> %s: %s", jid, mask(email), exc)
                if "authentication" in str(exc).lower():
                    break
                continue
            store.followup_record(email, jid, "sent")
            tracker.set(jid, {"Follow-up Status": SENT, "Follow-up Sent Timestamp": _now()})
            tracker.save()
            stats["sent"] += 1
            log.info("follow-up sent %s -> %s", jid, mask(email))
            if planned < cap:
                sleep(random.uniform(cfg.delay_min, cfg.delay_max))
    finally:
        if mailer_cm is not None:
            mailer_cm.__exit__(None, None, None)
        tracker.save()
    return stats
