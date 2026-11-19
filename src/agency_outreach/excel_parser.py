from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

import openpyxl

from .models import Lead
from .validators import extract_email, normalize_indian_mobile

log = logging.getLogger(__name__)


class ParseError(RuntimeError):
    """Raised when the Excel file cannot be read or contains no leads."""


def parse_leads(excel_path: str | Path) -> list[Lead]:
    if not excel_path:
        raise ParseError("No leads workbook given: pass --excel or set LEADS_EXCEL in .env")
    path = Path(excel_path)
    if not path.is_file():
        raise ParseError(f"Excel file not found: {path}")

    try:
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb["Leads"] if "Leads" in wb.sheetnames else wb.active
    except Exception as exc:
        raise ParseError(f"Could not read {path}: {exc}") from exc

    rows = list(ws.iter_rows(values_only=True))
    if not rows or len(rows) < 2:
        raise ParseError("Excel sheet is empty or contains only headers.")

    # Map headers dynamically (case-insensitive)
    raw_headers = [str(c or "").strip().lower() for c in rows[0]]
    header_map = {name: idx for idx, name in enumerate(raw_headers)}

    def get(row: Sequence[object], col_name: str, default: str = "") -> str:
        idx = header_map.get(col_name)
        if idx is None or idx >= len(row) or row[idx] is None:
            return default
        return str(row[idx]).strip()

    leads: list[Lead] = []
    for rank, row in enumerate(rows[1:], start=1):
        if not any(row):
            continue

        raw_email = get(row, "email")
        # Split in case multiple emails were stored separated by ';'
        email = None
        for candidate in raw_email.split(";"):
            email = extract_email(candidate)
            if email:
                break

        raw_phone = get(row, "phone")
        whatsapp = None
        for candidate in raw_phone.split(";"):
            whatsapp = normalize_indian_mobile(candidate)
            if whatsapp:
                break

        leads.append(
            Lead(
                rank=rank,
                name=get(row, "author name"),
                company=get(row, "company"),
                job_title=get(row, "job title") or "Python Developer",
                location=get(row, "location"),
                email=email,
                whatsapp=whatsapp,
                post_url=get(row, "post url"),
                profile_url=get(row, "profile url"),
                post_text=get(row, "post text"),
                is_it=True,
            )
        )

    if not leads:
        raise ParseError("No valid leads found in Excel file.")

    log.info(
        "Parsed %d leads (%d with email, %d WhatsApp-capable)",
        len(leads),
        sum(1 for l in leads if l.email),
        sum(1 for l in leads if l.whatsapp),
    )
    return leads