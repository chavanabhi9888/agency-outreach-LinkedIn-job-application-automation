"""Per-job e-mail drafts. Every claim comes from the master resume or from the job post itself."""
from __future__ import annotations

import re

from .config import Settings
from .job_analysis import JobAnalysis
from .resume_builder import matched_skill_names

_HDR_BAD = re.compile(r"[\r\n]+")


# Values that mean "no real data". Compared case-insensitively against the WHOLE field, so a
# placeholder like "Not Available" is rejected before we ever look at its first word.
_PLACEHOLDERS = {
    "", "not available", "not found", "not specified", "not provided", "n/a", "na", "none",
    "null", "nan", "unknown", "-", "--", "linkedin member",
}
_NOT_A_PERSON = re.compile(r"\d|@|hiring|team|talent|careers?|\bhr\b|recruit|human resources", re.IGNORECASE)
_TITLES = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam"}


def is_placeholder(value: str | None) -> bool:
    return re.sub(r"\s+", " ", (value or "").strip()).lower() in _PLACEHOLDERS


def _first_name(full: str) -> str | None:
    """Return a clean first name, or None when the field holds no real person's name."""
    full = re.sub(r"\s+", " ", (full or "").strip())
    if is_placeholder(full) or _NOT_A_PERSON.search(full) or len(full.split(" ")) > 4:
        return None
    parts = [t.strip(".,;:") for t in full.split(" ")]
    parts = [t for t in parts if t and t.lower() not in _TITLES]   # "Dr. Priya Nair" -> Priya
    if not parts:
        return None
    token = parts[0]
    if not re.fullmatch(r"[^\W\d_][\w'\u2019-]*", token) or len(token) < 2:   # letters (any script), no digits
        return None
    return token[:1].upper() + token[1:].lower() if token.islower() or token.isupper() else token


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def build_subject(job_title: str, company: str, cfg: Settings) -> str:
    title = _HDR_BAD.sub(" ", job_title or "Python Developer").strip()
    comp = _HDR_BAD.sub(" ", company or "").strip()
    subj = f"Application for {title}" + (f" at {comp}" if comp else "") + f" - {cfg.sender_name}"
    return subj[:150]


def build_body(
    *, recruiter_name: str, job_title: str, company: str, location: str,
    analysis: JobAnalysis, master: dict, cfg: Settings,
) -> str:
    wanted = set(analysis.tags)
    first = _first_name(recruiter_name)
    greeting = f"Hi {first}," if first else "Hello Hiring Team,"
    company = "" if is_placeholder(company) else company
    at_company = f" at {company.strip()}" if company and company.strip() else ""
    loc = re.sub(r"\s*\(.*?\)?\s*$|\s*\(.*?\)", "", location or "").strip(" ,-")  # drop "(Preferred)", "(Hybrid)" notes
    in_loc = f" in {loc}" if loc else ""
    matched = matched_skill_names(master, analysis)

    highlights = sorted(
        (h for h in master["summary_highlights"] if wanted & set(h["tags"])),
        key=lambda h: -len(wanted & set(h["tags"])),
    )[:2]

    paras = [f"I came across your LinkedIn post about the {job_title} opening{at_company}{in_loc} "
             f"and would like to express my interest."]

    lead = master["summary_lead"]  # "Python Backend Developer (3.5+ years)"
    if matched:
        paras.append(f"I am a {lead} and your post mentions {_join(matched[:6])}, "
                     f"which I work with hands-on.")
    else:
        paras.append(f"I am a {lead} working with Python, FastAPI, Django and PostgreSQL.")
    if highlights:
        paras[-1] += " For example, I " + "; I ".join(h["text"] for h in highlights) + "."

    paras.append(f"I am an {master.get('availability', 'Immediate Joiner')} based in {cfg.city} and "
                 f"would welcome the chance to discuss how I can contribute to your team.")
    paras.append("I have attached my resume, tailored to this role, for your review.")

    sign = [f"Best regards,", cfg.sender_name, cfg.phone, cfg.linkedin]
    return "\n\n".join([greeting, *paras, "Thank you for your time and consideration.", "\n".join(sign)])


def build_followup(row: dict[str, str], cfg: Settings) -> tuple[str, str]:
    """Short, honest follow-up to an application already sent. No new claims."""
    first = _first_name(row.get("Recruiter Name", ""))
    greeting = f"Hi {first}," if first else "Hello Hiring Team,"
    title = row.get("Job Title")
    title = "the open position" if is_placeholder(title) else title.strip()
    comp = row.get("Company Name", "")
    at_company = f" at {comp.strip()}" if not is_placeholder(comp) else ""
    sent_on = (row.get("Email Sent Timestamp") or "")[:10]
    when = f" on {sent_on}" if sent_on else ""
    subject = f"Following up: {row.get('Email Subject') or 'Application for ' + title}"[:150]
    body = "\n\n".join([
        greeting,
        f"I am following up on my application for the {title} role{at_company}, which I sent{when}. "
        f"I understand inboxes get busy, so I have attached my resume again for convenience.",
        "I remain very interested in the opportunity and would be glad to discuss my fit for the role "
        "or share any additional details you need.",
        "Thank you for your time.",
        "\n".join(["Best regards,", cfg.sender_name, cfg.phone, cfg.linkedin]),
    ])
    return subject, body