from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_IT_RE = re.compile(r"\b(IT|technology|software|digital|AI|data|cloud|DevOps)\b", re.IGNORECASE)


def extract_email(cell: str) -> str | None:
    """Return a lower-cased email found in a free-text cell, or None."""
    m = EMAIL_RE.search(cell or "")
    return m.group(0).lower() if m else None


def normalize_indian_mobile(raw: str) -> str | None:
    """Return '91XXXXXXXXXX' for an Indian mobile number, else None.

    Landlines (e.g. +91 22 4297 5100), toll-free (1800...) and malformed
    numbers return None because they cannot receive WhatsApp messages.
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        national = digits[2:]
    elif len(digits) == 10:
        national = digits
    else:
        return None
    return f"91{national}" if national[0] in "6789" else None


def is_it_agency(coverage: str) -> bool:
    return bool(_IT_RE.search(coverage or ""))
