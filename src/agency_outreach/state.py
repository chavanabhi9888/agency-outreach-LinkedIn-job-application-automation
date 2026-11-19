from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_log (
    email     TEXT PRIMARY KEY,
    agency    TEXT NOT NULL,
    status    TEXT NOT NULL CHECK (status IN ('sent','failed')),
    detail    TEXT,
    at_utc    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    job_id       TEXT PRIMARY KEY,
    email        TEXT NOT NULL,
    post_url     TEXT,
    resume_path  TEXT,
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('prepared','sending','sent','failed')),
    detail       TEXT,
    at_utc       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followups (
    email    TEXT PRIMARY KEY,
    job_id   TEXT NOT NULL,
    status   TEXT NOT NULL CHECK (status IN ('sending','sent','failed','rejected')),
    detail   TEXT,
    at_utc   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wa_log (
    number  TEXT PRIMARY KEY,
    agency  TEXT NOT NULL,
    at_utc  TEXT NOT NULL
);
"""


class StateStore:
    """SQLite log that makes runs idempotent: an address is never emailed twice."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def already_sent(self, email: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM email_log WHERE email=? AND status='sent'", (email.lower(),)
            ).fetchone()
        return row is not None

    def record(self, email: str, agency: str, status: str, detail: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO email_log(email, agency, status, detail, at_utc) VALUES (?,?,?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET agency=excluded.agency, status=excluded.status, "
                "detail=excluded.detail, at_utc=excluded.at_utc",
                (email.lower(), agency, status, detail[:500], now),
            )

    def summary(self) -> list[tuple[str, str, str, str]]:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT email, agency, status, at_utc FROM email_log ORDER BY at_utc"
            ).fetchall()

    def wa_done(self, number: str) -> bool:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT 1 FROM wa_log WHERE number=?", (number,)).fetchone() is not None

    def wa_record(self, number: str, agency: str) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO wa_log(number, agency, at_utc) VALUES (?,?,?)",
                         (number, agency, now))


    # ---- job applications (LinkedIn post -> tailored resume -> e-mail) ----
    def app_get(self, job_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def app_upsert(self, job_id: str, email: str, post_url: str, resume_path: str,
                   fingerprint: str, status: str, detail: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO applications(job_id,email,post_url,resume_path,fingerprint,status,detail,at_utc) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET email=excluded.email, "
                "post_url=excluded.post_url, resume_path=excluded.resume_path, fingerprint=excluded.fingerprint, "
                "status=excluded.status, detail=excluded.detail, at_utc=excluded.at_utc",
                (job_id, email.lower(), post_url, resume_path, fingerprint, status, detail[:500], now),
            )

    def app_set_status(self, job_id: str, status: str, detail: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE applications SET status=?, detail=?, at_utc=? WHERE job_id=?",
                         (status, detail[:500], now, job_id))

    # ---- one follow-up per recipient, ever ----
    def followup_done(self, email: str) -> bool:
        """True if a follow-up was sent, is mid-send (outcome unknown) or the address rejected mail."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT 1 FROM followups WHERE email=? AND status IN ('sending','sent','rejected')",
                               (email.lower(),)).fetchone()
        return row is not None

    def followup_record(self, email: str, job_id: str, status: str, detail: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO followups(email, job_id, status, detail, at_utc) VALUES (?,?,?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET job_id=excluded.job_id, status=excluded.status, "
                "detail=excluded.detail, at_utc=excluded.at_utc",
                (email.lower(), job_id, status, detail[:500], now),
            )
