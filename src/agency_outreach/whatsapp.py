"""WhatsApp support: click-to-chat links only.

WhatsApp's Business Platform forbids unsolicited bulk messaging, and browser
automation of personal accounts violates its Terms (numbers get banned). So this
module builds a wa.me queue you open and send yourself - one tap per agency.
"""
from __future__ import annotations

import html
from pathlib import Path
from urllib.parse import quote

from .config import Settings
from .models import Agency
from .templating import render


def build_link(number: str, text: str) -> str:
    return f"https://wa.me/{number}?text={quote(text)}"


def render_queue_html(agencies: list[Agency], cfg: Settings, template_dir: Path, out: Path) -> int:
    rows = []
    for a in agencies:
        if not a.whatsapp:
            continue
        link = build_link(a.whatsapp, render(template_dir / "whatsapp.txt", a, cfg))
        rows.append(
            f"<tr><td>{a.rank}</td><td>{html.escape(a.name)}</td>"
            f"<td>+{a.whatsapp}</td><td><a href=\"{html.escape(link)}\" target=\"_blank\" "
            f"rel=\"noopener\">Open chat</a></td><td><input type=\"checkbox\"></td></tr>"
        )
    page = (
        "<!doctype html><meta charset=utf-8><title>WhatsApp queue</title>"
        "<style>body{font-family:sans-serif;margin:2rem}td,th{padding:6px 12px;border-bottom:1px solid #ddd}</style>"
        "<h1>WhatsApp queue</h1><p>Open each chat, attach your resume PDF, press Send, tick the box.</p>"
        "<table><tr><th>#</th><th>Agency</th><th>Number</th><th>Link</th><th>Sent</th></tr>"
        + "".join(rows) + "</table>"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return len(rows)
