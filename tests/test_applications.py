import hashlib
import os
import re
from pathlib import Path

import openpyxl
import pdfplumber
import pytest

from agency_outreach import applications as app
from agency_outreach.config import Settings
from agency_outreach.email_sender import SendError
from agency_outreach.models import Lead
from agency_outreach.resume_builder import load_master, pdf_job_id
from agency_outreach.state import StateStore
from agency_outreach.tracker import Tracker, TrackerError

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = Path(os.getenv("RESUME_PATH", ROOT / "resume.pdf"))
MASTER = ROOT / "data" / "master_resume.json"
if not MASTER.exists():  # fresh clone: fall back to the sanitized example
    MASTER = ROOT / "data" / "master_resume.example.json"

JD1 = ("We are hiring a Python Developer at Acme. Skills: FastAPI, Kafka, PostgreSQL, Docker, Golang. "
       "3-5 years experience. Pune. Send your resume to hr@acme.com")
JD2 = "Backend role at Beta: Django, REST APIs, MySQL, Redis, Java. Email talent@beta.io with your CV."


@pytest.fixture
def cfg(tmp_path):
    return Settings(
        sender_name="Abhishek Chavan", sender_email="a@b.com", phone="+91 1", city="Pune",
        linkedin="https://l", github="https://g", resume_path=ORIGINAL, resume_url="",
        state_db=tmp_path / "s.db", delay_min=0, delay_max=0, max_per_run=50,
        master_resume=MASTER, resume_dir=tmp_path / "resumes", tracker_path=tmp_path / "t.xlsx",
    )


def lead(n=1, name="Priya Sharma", company="Acme", title="Python Developer", email=None, text=JD1, url=None):
    return Lead(n, name, company, title, "Pune", email, None,
                url or f"https://www.linkedin.com/feed/update/urn:li:activity:71234567890123456{n:02d}/", "", text)


class FakeMailer:
    sent: list = []
    fail_with: Exception | None = None

    def __init__(self, cfg): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass

    def send(self, msg):
        if FakeMailer.fail_with:
            raise FakeMailer.fail_with
        FakeMailer.sent.append(msg)


@pytest.fixture(autouse=True)
def reset_mailer():
    FakeMailer.sent, FakeMailer.fail_with = [], None


def run_prepare(cfg, leads, **kw):
    t, s = Tracker(cfg.tracker_path), StateStore(cfg.state_db)
    return t, s, app.prepare(leads, t, s, cfg, **kw)


def run_send(cfg, really=True, **kw):
    t, s = Tracker(cfg.tracker_path), StateStore(cfg.state_db)
    return app.send(t, s, cfg, really_send=really, mailer_factory=FakeMailer, sleep=lambda _: None, **kw)


def approve_all(cfg):
    app.approve(Tracker(cfg.tracker_path), None, True)


# ---------------------------------------------------------------- extraction / no-email
def test_post_without_email_is_marked_and_never_sent(cfg):
    t, s, stats = run_prepare(cfg, [lead(text="Python Developer needed, FastAPI, Docker, PostgreSQL. DM me.")])
    row = t.get(t.job_ids()[0])
    assert row["Recruiter Email"] == "Not Available"
    assert row["Email Sending Status"] == "Skipped" and row["Email Approval Status"] == "Skipped"
    assert row["Resume Generation Status"] == "Resume Generated"
    approve_all(cfg)
    assert run_send(cfg).get("sent", 0) == 0 and FakeMailer.sent == []


def test_email_taken_from_post_text_when_column_empty(cfg):
    t, *_ = run_prepare(cfg, [lead()])
    assert t.get(t.job_ids()[0])["Recruiter Email"] == "hr@acme.com"


def test_incomplete_jd_skipped_with_reason(cfg):
    t, *_ = run_prepare(cfg, [lead(text="hiring")])
    row = t.get(t.job_ids()[0])
    assert row["Resume Generation Status"] == "Skipped" and "description" in row["Error Details"].lower()


# ---------------------------------------------------------------- resume
def _pdf_text(path):
    with pdfplumber.open(path) as p:
        return "\n".join(pg.extract_text() or "" for pg in p.pages)


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


@pytest.mark.skipif(not ORIGINAL.exists(), reason="original resume PDF not present")
def test_master_data_matches_original_resume_pdf():
    m, original = load_master(MASTER), _norm(_pdf_text(ORIGINAL))
    lines = [i["text"] for c in m["skills"] for i in c["items"]]
    for j in m["experience"]:
        lines += [b["text"] for b in j["bullets"]] + [j["company"], j["title"], j["technologies"] if "technologies" in j else ""]
        lines += [b["text"] for x in j.get("extra_sections", []) for b in x["bullets"]]
    for p in m["projects"]:
        lines += [p["title"]] + [b["text"] for b in p["bullets"]]
    assert [l for l in lines if _norm(l) not in original] == []


def test_resume_is_factual_named_and_original_untouched(cfg):
    before = hashlib.sha256(ORIGINAL.read_bytes()).hexdigest()
    t, *_ = run_prepare(cfg, [lead()])
    row = t.get(t.job_ids()[0])
    path = Path(row["Customized Resume Path"])
    assert path.name == "Abhishek_Chavan_3_5_years_Acme.pdf" and path.is_file()
    text, original = _pdf_text(path), _norm(_pdf_text(ORIGINAL))
    # requested-but-unsupported skill must not appear; all dates/employers preserved verbatim
    assert "golang" not in text.lower()
    for fact in ["Jan 2025 – Sep 2026", "Feb 2023 – Aug 2024", "Nov 2022 – Jan 2023", "GoDigit General Insurance",
                 "Setoo Solutions", "Geexu Technology", "2018 – 2021"]:
        assert fact in text
    # every bullet in the tailored PDF is a bullet from the original (reordered only)
    bullets = [l[2:].strip() for l in text.splitlines() if l.startswith("- ")]
    assert bullets
    for b in bullets:
        assert _norm(b)[:60] in original
    assert hashlib.sha256(ORIGINAL.read_bytes()).hexdigest() == before
    assert pdf_job_id(path) == row["Job ID"]


def test_relevant_experience_is_reordered_first(cfg):
    t, *_ = run_prepare(cfg, [lead(text="Kafka messaging engineer, Kafka, microservices, notification pipelines")])
    text = _pdf_text(t.get(t.job_ids()[0])["Customized Resume Path"])
    assert text.index("Apache Kafka-integrated") < text.index("Containerized backend services")


def test_distinct_jobs_with_same_title_and_company_get_distinct_files(cfg):
    t, *_ = run_prepare(cfg, [lead(1), lead(2, text=JD1 + " second opening")])
    paths = {t.get(j)["Customized Resume Path"] for j in t.job_ids()}
    assert len(paths) == 2


# ---------------------------------------------------------------- email draft
def test_email_draft_is_personalised_and_supported(cfg):
    t, *_ = run_prepare(cfg, [lead()])
    row = t.get(t.job_ids()[0])
    body, subject = row["Email Body or Template"], row["Email Subject"]
    assert "Python Developer" in subject and "Acme" in subject
    assert body.startswith("Hi Priya,") and "Acme" in body and "attached" in body
    assert "FastAPI" in body and "Kafka" in body
    assert "Golang" not in body and "Java" not in body      # not in the real resume -> never claimed
    t2, *_ = run_prepare(cfg, [lead(2, name="", company="Beta", title="Backend Engineer", text=JD2)])
    other = t2.get(app.analyze_job(lead(2).post_url, "", "", "").job_id)["Email Body or Template"]
    assert other.startswith("Hello Hiring Team,") and "Django" in other and "Kafka" not in other


# ---------------------------------------------------------------- approval workflow
def test_nothing_is_sent_without_approval_or_without_send_flag(cfg):
    run_prepare(cfg, [lead()])
    row = Tracker(cfg.tracker_path).get(Tracker(cfg.tracker_path).job_ids()[0])
    assert row["Email Approval Status"] == "Awaiting Approval"
    assert run_send(cfg).get("sent", 0) == 0 and FakeMailer.sent == []      # not approved
    approve_all(cfg)
    assert run_send(cfg, really=False).get("would_send") == 1 and FakeMailer.sent == []  # dry-run
    assert run_send(cfg).get("sent") == 1 and len(FakeMailer.sent) == 1


def test_auto_send_setting_preapproves_but_still_needs_send_flag(cfg):
    from dataclasses import replace
    cfg2 = replace(cfg, auto_send=True)
    t, *_ = run_prepare(cfg2, [lead()])
    assert t.get(t.job_ids()[0])["Email Approval Status"] == "Approved"
    assert run_send(cfg2, really=False).get("would_send") == 1 and FakeMailer.sent == []


def test_approving_in_excel_directly_works(cfg):
    t, *_ = run_prepare(cfg, [lead()])
    t.set(t.job_ids()[0], {"Email Approval Status": "Approved"}); t.save()
    assert run_send(cfg).get("sent") == 1


# ---------------------------------------------------------------- matching & sending
def test_each_email_carries_its_own_resume_and_recipient(cfg):
    run_prepare(cfg, [lead(1), lead(2, company="Beta", title="Django Developer", text=JD2)])
    approve_all(cfg)
    assert run_send(cfg).get("sent") == 2
    by_to = {m["To"]: m for m in FakeMailer.sent}
    assert set(by_to) == {"hr@acme.com", "talent@beta.io"}
    assert [p.get_filename() for p in by_to["hr@acme.com"].iter_attachments()] == ["Abhishek_Chavan_3_5_years_Acme.pdf"]
    assert [p.get_filename() for p in by_to["talent@beta.io"].iter_attachments()] == ["Abhishek_Chavan_3_5_years_Beta.pdf"]
    assert by_to["hr@acme.com"]["Cc"] is None and by_to["hr@acme.com"]["Bcc"] is None


def test_swapped_resume_or_recipient_is_blocked(cfg):
    t, *_ = run_prepare(cfg, [lead(1), lead(2, company="Beta", title="Django Developer", text=JD2)])
    a, b = t.job_ids()
    ra, rb = t.get(a)["Customized Resume Path"], t.get(b)["Customized Resume Path"]
    t.set(a, {"Customized Resume Path": rb})                 # wrong resume for job a
    t.set(b, {"Recruiter Email": "someone.else@evil.com"})   # tampered recipient for job b
    t.save(); approve_all(cfg)
    stats = run_send(cfg)
    assert stats.get("sent", 0) == 0 and FakeMailer.sent == []
    t = Tracker(cfg.tracker_path)
    assert t.get(a)["Email Sending Status"] == "Failed" and t.get(b)["Email Sending Status"] == "Failed"


def test_duplicate_job_and_duplicate_recipient_prevented(cfg):
    t, *_ = run_prepare(cfg, [lead(1), lead(1)])             # same post twice -> one row
    assert len(t) == 1
    approve_all(cfg); run_send(cfg)
    assert run_send(cfg).get("sent", 0) == 0 and len(FakeMailer.sent) == 1
    # a different job posted by the same recruiter address
    run_prepare(cfg, [lead(3, text=JD1 + " another role")])
    approve_all(cfg)
    stats = run_send(cfg)
    assert stats.get("duplicates_skipped") == 1 and len(FakeMailer.sent) == 1
    t = Tracker(cfg.tracker_path)
    assert "already sent" in t.get(t.job_ids()[-1])["Error Details"].lower()


def test_recipient_duplicate_allowed_only_when_configured(cfg):
    from dataclasses import replace
    run_prepare(cfg, [lead(1)]); approve_all(cfg); run_send(cfg)
    cfg2 = replace(cfg, allow_multiple_per_recipient=True)
    run_prepare(cfg2, [lead(3, text=JD1 + " another role")]); approve_all(cfg2)
    assert run_send(cfg2).get("sent") == 1


def test_already_emailed_address_in_legacy_log_is_not_emailed_again(cfg):
    StateStore(cfg.state_db).record("hr@acme.com", "Acme", "sent")
    run_prepare(cfg, [lead()]); approve_all(cfg)
    assert run_send(cfg).get("duplicates_skipped") == 1 and FakeMailer.sent == []


# ---------------------------------------------------------------- failures & recovery
def test_smtp_failure_marks_row_and_batch_continues(cfg):
    run_prepare(cfg, [lead(1), lead(2, company="Beta", title="Django Developer", text=JD2)]); approve_all(cfg)

    class Flaky(FakeMailer):
        n = 0
        def send(self, msg):
            Flaky.n += 1
            if Flaky.n == 1:
                raise SendError("Recipient refused: x")
            super().send(msg)

    t, s = Tracker(cfg.tracker_path), StateStore(cfg.state_db)
    stats = app.send(t, s, cfg, really_send=True, mailer_factory=Flaky, sleep=lambda _: None)
    assert stats["failed"] == 1 and stats["sent"] == 1
    t = Tracker(cfg.tracker_path)
    states = {t.get(j)["Email Sending Status"]: t.get(j)["Error Details"] for j in t.job_ids()}
    assert "refused" in states["Failed"].lower() and "Sent" in states
    # failed rows are retried only on request
    assert run_send(cfg).get("sent", 0) == 0
    assert run_send(cfg, retry_failed=True).get("sent") == 1


def test_authentication_error_aborts_batch(cfg):
    run_prepare(cfg, [lead(1), lead(2, company="Beta", title="Django Developer", text=JD2)]); approve_all(cfg)
    FakeMailer.fail_with = SendError("SMTP authentication failed. use app password")
    stats = run_send(cfg)
    assert stats["failed"] == 1 and stats.get("sent", 0) == 0


def test_missing_resume_file_fails_that_row_only(cfg):
    t, *_ = run_prepare(cfg, [lead(1), lead(2, company="Beta", title="Django Developer", text=JD2)])
    Path(t.get(t.job_ids()[0])["Customized Resume Path"]).unlink()
    approve_all(cfg)
    stats = run_send(cfg)
    assert stats["failed"] == 1 and stats["sent"] == 1


def test_interrupted_send_is_never_blindly_resent(cfg):
    t, *_ = run_prepare(cfg, [lead()])
    jid = t.job_ids()[0]
    approve_all(cfg)
    t = Tracker(cfg.tracker_path); t.set(jid, {"Email Sending Status": "Sending"}); t.save()
    StateStore(cfg.state_db).app_set_status(jid, "sending")
    stats = run_send(cfg)
    assert stats["recovered_interrupted"] == 1 and FakeMailer.sent == []
    row = Tracker(cfg.tracker_path).get(jid)
    assert row["Email Sending Status"] == "Failed" and "Sent folder" in row["Error Details"]


def test_prepare_is_idempotent_and_never_resets_sent_rows(cfg):
    run_prepare(cfg, [lead()]); approve_all(cfg); run_send(cfg)
    t, s, stats = run_prepare(cfg, [lead()])
    assert stats["already_prepared"] == 1
    row = t.get(t.job_ids()[0])
    assert row["Email Sending Status"] == "Sent" and row["Email Sent Timestamp"]
    assert run_send(cfg).get("sent", 0) == 0


def test_one_bad_record_does_not_stop_prepare(cfg, monkeypatch):
    real = app.build_resume
    def boom(master, analysis, title, company, out):
        if company == "Bad":
            raise RuntimeError("kaboom")
        return real(master, analysis, title, company, out)
    monkeypatch.setattr(app, "build_resume", boom)
    t, _, stats = run_prepare(cfg, [lead(1, company="Bad"), lead(2, company="Good", text=JD2)])
    assert stats["failed"] == 1 and stats["resumes_generated"] == 1
    statuses = {t.get(j)["Company Name"]: t.get(j)["Resume Generation Status"] for j in t.job_ids()}
    assert statuses == {"Bad": "Failed", "Good": "Resume Generated"}


# ---------------------------------------------------------------- Excel
def test_tracker_columns_and_persistence(cfg):
    run_prepare(cfg, [lead()]); approve_all(cfg); run_send(cfg)
    ws = openpyxl.load_workbook(cfg.tracker_path)["Applications"]
    headers = [c.value for c in ws[1]]
    for col in ["LinkedIn Post URL", "Company Name", "Job Title", "Job Description", "Required Skills", "Job Location",
                "Recruiter Email", "Customized Resume Path", "Email Subject", "Email Body or Template",
                "Resume Generation Status", "Email Approval Status", "Email Sending Status",
                "Email Sent Timestamp", "Error Details"]:
        assert col in headers
    assert ws.max_row == 2


def test_corrupt_tracker_raises_clear_error_and_is_not_overwritten(cfg):
    cfg.tracker_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.tracker_path.write_bytes(b"not an xlsx")
    with pytest.raises(TrackerError):
        Tracker(cfg.tracker_path)
    assert cfg.tracker_path.read_bytes() == b"not an xlsx"


def test_location_notes_are_not_pasted_into_the_email(cfg):
    from dataclasses import replace
    l = replace(lead(), location="Pune, Maharashtra (Preferred)")
    t, *_ = run_prepare(cfg, [l])
    body = t.get(t.job_ids()[0])["Email Body or Template"]
    assert "opening at Acme in Pune, Maharashtra and" in body and "(Preferred)" not in body


def test_resume_file_naming_with_and_without_company(cfg):
    from agency_outreach.resume_builder import resume_filename
    assert resume_filename("Abhishek Chavan", "AMPLE Enterprise Technologies") == "Abhishek_Chavan_3_5_years_AMPLE_Enterprise_Technologies.pdf"
    for blank in ["", "  ", "Not Available"]:
        assert resume_filename("Abhishek Chavan", blank) == "Abhishek_Chavan_3_5_years_Ai_Automation.pdf"
    t, *_ = run_prepare(cfg, [lead(1, company=""), lead(2, company="", text=JD1 + " second")])
    rows = [t.get(j) for j in t.job_ids()]
    assert {Path(r["Customized Resume Path"]).name for r in rows} == {"Abhishek_Chavan_3_5_years_Ai_Automation.pdf"}
    assert len({r["Customized Resume Path"] for r in rows}) == 2   # separate folders, no overwrite


def test_already_emailed_rows_do_not_use_up_the_send_cap(cfg):
    from dataclasses import replace
    cfg2 = replace(cfg, max_per_run=2)
    store = StateStore(cfg2.state_db)
    old = [lead(n, text=f"Python FastAPI Docker Kafka PostgreSQL role. Send CV to old{n}@x.com") for n in range(1, 6)]
    new = [lead(n, text=f"Python FastAPI Docker Kafka PostgreSQL role. Send CV to new{n}@x.com") for n in range(6, 11)]
    for n in range(1, 6):
        store.record(f"old{n}@x.com", "X", "sent")          # already e-mailed in an earlier run
    run_prepare(cfg2, old + new); approve_all(cfg2)
    stats = run_send(cfg2)
    assert stats["sent"] == 2 and stats["left_for_next_run"] == 3 and stats["duplicates_skipped"] == 5
    assert sorted(m["To"] for m in FakeMailer.sent) == ["new6@x.com", "new7@x.com"]
    assert run_send(cfg2).get("sent") == 2 and run_send(cfg2).get("sent") == 1   # remaining 3 over next runs


def test_review_and_approve_all_exclude_duplicates(cfg):
    store = StateStore(cfg.state_db)
    store.record("old1@x.com", "X", "sent")
    text = "Python FastAPI Docker Kafka PostgreSQL role. Send CV to {}"
    leads = [lead(1, text=text.format("old1@x.com")), lead(2, text=text.format("a@x.com")),
             lead(3, text=text.format("a@x.com")), lead(4, text=text.format("b@x.com"))]
    t, *_ = run_prepare(cfg, leads)
    rows = app.pending_review(t, store, cfg)
    assert sorted(r["Recruiter Email"] for r in rows) == ["a@x.com", "b@x.com"]   # old + repeated address removed
    assert app.approve(t, None, True, store, cfg) == 2


def test_excluded_domains_are_never_emailed_or_approved(cfg):
    from dataclasses import replace
    cfg2 = replace(cfg, skip_domains=("gmail.com",))
    text = "Python FastAPI Docker Kafka PostgreSQL role. Send CV to {}"
    leads = [lead(1, text=text.format("someone@gmail.com")), lead(2, text=text.format("hr@acme.com")),
             lead(3, text=text.format("x@mail.gmail.com"))]
    t, *_ = run_prepare(cfg2, leads)
    assert app.approve(t, None, True, StateStore(cfg2.state_db), cfg2) == 1
    stats = run_send(cfg2)
    assert [m["To"] for m in FakeMailer.sent] == ["hr@acme.com"] and stats["sent"] == 1
    t = Tracker(cfg2.tracker_path)
    reasons = [t.get(j)["Error Details"] for j in t.job_ids() if t.get(j)["Email Sending Status"] == "Skipped"]
    assert len(reasons) == 2 and all("domain excluded" in r for r in reasons)
    # even if someone force-approves a gmail row in Excel, the send step still refuses it
    t.set(t.job_ids()[0], {"Email Approval Status": "Approved", "Email Sending Status": "Pending"}); t.save()
    FakeMailer.sent.clear()
    assert run_send(cfg2).get("sent", 0) == 0 and FakeMailer.sent == []


def test_send_all_ignores_the_per_run_cap(cfg):
    from dataclasses import replace
    cfg2 = replace(cfg, max_per_run=2)
    text = "Python FastAPI Docker Kafka PostgreSQL role. Send CV to n{}@x.com"
    run_prepare(cfg2, [lead(n, text=text.format(n)) for n in range(1, 6)]); approve_all(cfg2)
    assert run_send(cfg2, send_all=True).get("sent") == 5


def test_gmail_recipients_are_excluded_and_everyone_else_is_sent_without_cap(cfg):
    from dataclasses import replace
    cfg2 = replace(cfg, max_per_run=2)
    text = "Python FastAPI Docker Kafka PostgreSQL role. Send CV to {}"
    addrs = ["hr@acme.com", "x@gmail.com", "Y@GMAIL.COM", "a@b.io", "c@d.in", "e@f.org", "g@notgmail.com"]
    t, *_ = run_prepare(cfg2, [lead(n, text=text.format(a)) for n, a in enumerate(addrs, 1)])
    approve_all(cfg2)
    stats = run_send(cfg2, send_all=True)
    assert sorted(m["To"] for m in FakeMailer.sent) == ["a@b.io", "c@d.in", "e@f.org", "g@notgmail.com", "hr@acme.com"]
    assert stats["sent"] == 5 and stats["duplicates_skipped"] == 2
    t = Tracker(cfg2.tracker_path)
    errs = [r["Error Details"] for r in (t.get(j) for j in t.job_ids()) if r["Email Sending Status"] == "Skipped"]
    assert len(errs) == 2 and all("excluded" in e for e in errs)


def test_excluded_domains_configurable(cfg):
    from dataclasses import replace
    cfg2 = replace(cfg, skip_domains=())
    run_prepare(cfg2, [lead(1, text="Python FastAPI Docker Kafka role. Send CV to z@gmail.com")]); approve_all(cfg2)
    assert run_send(cfg2).get("sent") == 1


# ---------------------------------------------------------------- follow-ups
def _send_first_round(cfg, leads):
    run_prepare(cfg, leads); approve_all(cfg); run_send(cfg)
    FakeMailer.sent.clear()


def _age(cfg, days_ago, jid=None):
    """Pretend the first e-mail went out `days_ago` days ago."""
    from datetime import datetime, timedelta, timezone
    t = Tracker(cfg.tracker_path)
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    for j in ([jid] if jid else t.job_ids()):
        t.set(j, {"Email Sent Timestamp": ts})
    t.save()


def run_followup(cfg, days=14, really=True, **kw):
    t, s = Tracker(cfg.tracker_path), StateStore(cfg.state_db)
    return app.followup(t, s, cfg, days=days, really_send=really, mailer_factory=FakeMailer, sleep=lambda _: None, **kw)


def test_followup_goes_only_to_old_enough_applications_once(cfg):
    _send_first_round(cfg, [lead(1), lead(2, company="Beta", title="Django Developer", text=JD2)])
    t = Tracker(cfg.tracker_path); a, b = t.job_ids()
    _age(cfg, 20, a); _age(cfg, 3, b)
    stats = run_followup(cfg)
    assert stats["sent"] == 1 and stats["too_recent"] == 1
    m = FakeMailer.sent[0]
    assert m["To"] == "hr@acme.com" and m["Subject"].startswith("Following up:")
    assert [p.get_filename() for p in m.iter_attachments()] == ["Abhishek_Chavan_3_5_years_Acme.pdf"]
    body = m.get_body().get_content()
    assert "following up" in body and "Acme" in body
    assert run_followup(cfg).get("sent", 0) == 0                      # only ever once per recruiter
    row = Tracker(cfg.tracker_path).get(a)
    assert row["Follow-up Status"] == "Sent" and row["Follow-up Sent Timestamp"]


def test_followup_dry_run_sends_nothing_and_replied_or_gmail_are_excluded(cfg):
    from dataclasses import replace
    text = "Python FastAPI Docker Kafka PostgreSQL role. Send CV to {}"
    cfg2 = replace(cfg, skip_domains=())             # allow gmail for the first round, exclude for the follow-up
    _send_first_round(cfg2, [lead(1, text=text.format("a@x.com")), lead(2, text=text.format("b@x.com")),
                             lead(3, text=text.format("c@gmail.com"))])
    _age(cfg2, 30)
    t = Tracker(cfg2.tracker_path)
    t.set(t.job_ids()[1], {"Replied": "Yes"}); t.save()
    assert run_followup(cfg2, really=False)["would_send"] == 2 and FakeMailer.sent == []   # dry run: a@ and c@
    stats = run_followup(cfg)                          # default cfg excludes gmail.com
    assert stats["sent"] == 1 and stats["replied_excluded"] == 1 and stats["excluded_domain"] == 1
    assert [m["To"] for m in FakeMailer.sent] == ["a@x.com"]


def test_followup_failure_marks_row_and_unknown_outcome_is_not_resent(cfg):
    _send_first_round(cfg, [lead(1)]); _age(cfg, 30)
    FakeMailer.fail_with = SendError("Recipient refused: x")
    assert run_followup(cfg)["failed"] == 1
    FakeMailer.fail_with = None
    assert run_followup(cfg).get("sent", 0) == 0          # permanent rejection is not retried
    # interrupted mid-send: recorded as 'sending' -> never blindly re-sent
    _send_first_round(cfg, [lead(2, text=JD1.replace("hr@acme.com", "q@z.com"))]); _age(cfg, 30)
    jid = [j for j in Tracker(cfg.tracker_path).job_ids() if Tracker(cfg.tracker_path).get(j)["Recruiter Email"] == "q@z.com"][0]
    StateStore(cfg.state_db).followup_record("q@z.com", jid, "sending")
    assert run_followup(cfg).get("sent", 0) == 0 and FakeMailer.sent == []
