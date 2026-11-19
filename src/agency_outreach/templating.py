from __future__ import annotations

from pathlib import Path
from string import Template

from .config import Settings
from .models import Lead


def render(template_path: Path, lead: Lead, cfg: Settings) -> str:
    """Render a string.Template file with lead & config variables."""
    # 1. Natural greeting name
    if lead.name and lead.name.strip():
        greeting_name = lead.name.strip()
    elif lead.company and lead.company.strip():
        greeting_name = f"{lead.company.strip()} Team"
    else:
        greeting_name = "there"

    # 2. Company phrasing (avoids "opening at your company")
    company_phrase = f" at {lead.company.strip()}" if lead.company and lead.company.strip() else ""

    # 3. Resume line
    if cfg.resume_url:
        resume_line = f"📄 Resume: {cfg.resume_url}"
    else:
        resume_line = "📄 Resume: (Attaching my PDF right here)"

    return Template(template_path.read_text(encoding="utf-8")).safe_substitute(
        # Recipient & Job context
        greeting_name=greeting_name,
        recipient_name=lead.display_name,
        agency_name=lead.display_name,
        author_name=lead.name or "Hiring Manager",
        company=lead.company or "",
        company_phrase=company_phrase,
        job_title=lead.job_title or "Python Developer",
        location=lead.location or cfg.city,
        post_url=lead.post_url,
        # Sender details
        sender_name=cfg.sender_name,
        phone=cfg.phone,
        city=cfg.city,
        linkedin=cfg.linkedin,
        github=cfg.github,
        resume_line=resume_line,
    ).strip()