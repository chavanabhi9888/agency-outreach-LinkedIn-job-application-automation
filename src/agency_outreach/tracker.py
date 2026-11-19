"""Excel tracker: one row per job (keyed by Job ID), updated and saved atomically after each step."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font

COLUMNS = [
    "Job ID", "LinkedIn Post URL", "Company Name", "Job Title", "Job Description", "Required Skills",
    "Experience Required", "Job Location", "Application Instructions", "Recruiter Name", "Recruiter Email",
    "Customized Resume Path", "Email Subject", "Email Body or Template", "Resume Generation Status",
    "Email Approval Status", "Email Sending Status", "Email Sent Timestamp", "Error Details", "Draft Fingerprint",
    "Replied", "Follow-up Status", "Follow-up Sent Timestamp",
]
SHEET = "Applications"
CELL_LIMIT = 32000  # Excel hard limit is 32,767 characters per cell


class TrackerError(RuntimeError):
    pass


class Tracker:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._index: dict[str, int] = {}
        self._cols: dict[str, int] = {}
        if self.path.exists():
            try:
                self.wb = openpyxl.load_workbook(self.path)
            except Exception as exc:  # corrupt / locked / not xlsx
                raise TrackerError(f"Could not read tracker {self.path}: {exc}") from exc
            self.ws = self.wb[SHEET] if SHEET in self.wb.sheetnames else self.wb.create_sheet(SHEET)
        else:
            self.wb = openpyxl.Workbook()
            self.ws = self.wb.active
            self.ws.title = SHEET
        self._ensure_headers()
        for r in range(2, self.ws.max_row + 1):
            jid = self.ws.cell(r, self._cols["Job ID"]).value
            if jid:
                self._index[str(jid)] = r

    def _ensure_headers(self) -> None:
        existing = {str(c.value): c.column for c in self.ws[1] if c.value}
        col = max(existing.values(), default=0)
        for name in COLUMNS:
            if name not in existing:
                col += 1
                cell = self.ws.cell(1, col, name)
                cell.font = Font(bold=True)
                existing[name] = col
        self._cols = existing
        self.ws.freeze_panes = "A2"

    def __len__(self) -> int:
        return len(self._index)

    def has(self, job_id: str) -> bool:
        return job_id in self._index

    def get(self, job_id: str) -> dict[str, str]:
        r = self._index.get(job_id)
        if r is None:
            return {}
        return {name: ("" if (v := self.ws.cell(r, c).value) is None else str(v)) for name, c in self._cols.items()}

    def job_ids(self) -> list[str]:
        return list(self._index)

    def set(self, job_id: str, fields: dict[str, str]) -> None:
        r = self._index.get(job_id)
        if r is None:
            r = self.ws.max_row + 1 if self._index or self.ws.max_row > 1 else 2
            self._index[job_id] = r
            self.ws.cell(r, self._cols["Job ID"], job_id)
        for name, value in fields.items():
            if name not in self._cols:
                raise TrackerError(f"Unknown tracker column: {name}")
            text = "" if value is None else str(value)
            cell = self.ws.cell(r, self._cols[name], text[:CELL_LIMIT])
            if name in ("Job Description", "Email Body or Template"):
                cell.alignment = Alignment(wrap_text=False, vertical="top")

    def save(self) -> None:
        """Atomic save: write a temp file beside the target, then replace it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=self.path.parent)
        os.close(fd)
        try:
            self.wb.save(tmp)
            os.replace(tmp, self.path)
        except Exception as exc:
            Path(tmp).unlink(missing_ok=True)
            raise TrackerError(f"Could not write tracker {self.path}: {exc}") from exc
