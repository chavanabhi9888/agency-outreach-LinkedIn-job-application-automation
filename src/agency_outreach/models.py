from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Lead:
    """One row from leads.xlsx."""

    rank: int
    name: str              # Author name
    company: str           # Company
    job_title: str         # Job title
    location: str          # Location
    email: str | None      # First validated email
    whatsapp: str | None   # Normalized 12-digit Indian mobile (91XXXXXXXXXX)
    post_url: str          # LinkedIn post link
    profile_url: str       # Author profile link
    post_text: str         # Full post text
    is_it: bool = True

    @property
    def display_name(self) -> str:
        if self.name and self.name.strip():
            return self.name.strip()
        if self.company and self.company.strip():
            return f"{self.company.strip()} Hiring Team"
        return "Hiring Team"


# Alias for backwards compatibility across existing modules
Agency = Lead