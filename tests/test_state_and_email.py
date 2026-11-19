from pathlib import Path

import pytest
from agency_outreach.cli import select_email_targets
from agency_outreach.config import Settings
from agency_outreach.email_sender import SendError, build_message
from agency_outreach.models import Lead
from agency_outreach.state import StateStore

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def agency(name="Acme HR", email="hr@acme.in", it=True):
    # Lead model (rank, name, company, job_title, location, email, whatsapp, post_url, profile_url, post_text)
    return Lead(1, name, "Acme", "Python Developer", "Pune", email, None, "https://x/p", "", "", it)


@pytest.fixture
def cfg(tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    return Settings(sender_name="A B", sender_email="a@b.com", phone="+91 1", city="Pune",
                    linkedin="https://l", github="https://g", resume_path=resume, resume_url="https://drive.example/r",
                    state_db=tmp_path / "s.db", delay_min=0, delay_max=0, max_per_run=5)


def test_state_idempotent(tmp_path):
    s = StateStore(tmp_path / "x.db")
    assert not s.already_sent("A@x.com")
    s.record("A@x.com", "X", "failed", "boom")
    assert not s.already_sent("a@x.com")
    s.record("a@x.com", "X", "sent")
    assert s.already_sent("A@X.COM")


def test_message_has_attachment_single_recipient(cfg):
    m = build_message(agency(), cfg, TEMPLATES)
    assert m["To"] == "hr@acme.in" and m["Bcc"] is None and m["Cc"] is None
    assert "Acme HR" in m.get_body().get_content()
    assert [p.get_filename() for p in m.iter_attachments()] == ["resume.pdf"]


def test_missing_resume(cfg, tmp_path):
    bad = Settings(**{**cfg.__dict__, "resume_path": tmp_path / "none.pdf"})
    with pytest.raises(SendError):
        build_message(agency(), bad, TEMPLATES)


def test_target_selection_dedup_and_filter():
    a = [agency("A", "x@y.in"), agency("B", "x@y.in"), agency("C", None), agency("D", "d@y.in", it=False)]
    # de-duplicated by address, leads without a valid email dropped
    assert [t.name for t in select_email_targets(a, False)] == ["A", "D"]


def test_auth_failure_is_clean_error(cfg, monkeypatch):
    import smtplib
    from agency_outreach.email_sender import SmtpMailer

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def starttls(self, **k): pass
        def login(self, u, p): raise smtplib.SMTPAuthenticationError(534, b"App password required")

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    with pytest.raises(SendError, match="APP PASSWORD"):
        with SmtpMailer(Settings(**{**cfg.__dict__, "smtp_host": "smtp.gmail.com", "smtp_user": "a@b.com", "smtp_password": "x"})):
            pass


def test_whatsapp_message_and_state(cfg, tmp_path):
    from agency_outreach.templating import render
    from agency_outreach.whatsapp import build_link
    text = render(TEMPLATES / "whatsapp.txt", agency(), cfg)
    assert "https://drive.example/r" in text and "Acme HR" in text
    assert build_link("919884266639", text).startswith("https://wa.me/919884266639?text=")
    s = StateStore(tmp_path / "w.db")
    assert not s.wa_done("919884266639")
    s.wa_record("919884266639", "Acme HR")
    assert s.wa_done("919884266639")
