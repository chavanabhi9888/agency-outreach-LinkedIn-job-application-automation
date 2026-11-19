from agency_outreach.job_analysis import (
    NOT_AVAILABLE, analyze_job, extract_emails, job_id_for, normalize_post_url, pick_recruiter_email,
)


def test_extracts_plain_and_obfuscated_emails():
    text = "Send CV to Hiring.Team@Acme.com or careers [at] beta [dot] co [dot] in. noreply@acme.com ignore. logo@x.png"
    assert extract_emails(text) == ["hiring.team@acme.com", "careers@beta.co.in"]


def test_never_guesses_an_email():
    assert extract_emails("Apply at www.acme.com/careers, contact our HR team") == []
    assert pick_recruiter_email("No contact details here") == (NOT_AVAILABLE, "none")


def test_column_value_preferred_then_post_text():
    assert pick_recruiter_email("mail jobs@a.com", "hr@b.com") == ("hr@b.com", "email column")
    assert pick_recruiter_email("mail jobs@a.com", "") == ("jobs@a.com", "post text")


def test_job_id_is_stable_for_url_variants():
    a = "https://www.linkedin.com/feed/update/urn:li:activity:7123456789012345678/?utm=1"
    b = "https://linkedin.com/feed/update/urn:li:activity:7123456789012345678"
    assert job_id_for(a) == job_id_for(b) == "li-7123456789012345678"
    u1 = "https://www.linkedin.com/posts/abc-123/?x=1#frag"
    u2 = "https://linkedin.com/posts/abc-123"
    assert normalize_post_url(u1) == normalize_post_url(u2)
    assert job_id_for(u1) == job_id_for(u2)
    assert job_id_for(u1) != job_id_for("https://linkedin.com/posts/other")


def test_analysis_extracts_skills_experience_instructions():
    a = analyze_job("u", "Backend Dev", "Acme",
                    "Need FastAPI, Kafka and PostgreSQL. 3-5 years experience.\nSend your resume to hr@acme.com")
    assert {"fastapi", "kafka", "postgresql"} <= set(a.tags)
    assert a.experience == "3-5 years"
    assert "resume" in a.instructions.lower()
    assert a.complete


def test_incomplete_job_description_flagged():
    a = analyze_job("u", "Dev", "Acme", "hiring")
    assert not a.complete and a.problem
