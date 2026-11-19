"""Tailor the master resume to a job and render an ATS-friendly PDF.

Hard rule: this module only SELECTS and REORDERS content that already exists in the master
resume JSON. It never writes new claims, so skills the job asks for but you lack are simply
not mentioned. Employment dates, titles, companies and education are copied verbatim.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer

from .job_analysis import JobAnalysis


class ResumeError(RuntimeError):
    pass


def load_master(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ResumeError(f"Master resume data not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResumeError(f"Could not read master resume data {p}: {exc}") from exc


def _score(tags: list[str], wanted: set[str]) -> int:
    return sum(1 for t in tags if t in wanted)


def tailor(master: dict, analysis: JobAnalysis) -> dict:
    """Return a re-ordered deep copy of the master. Content is never added."""
    wanted = set(analysis.tags)
    r = copy.deepcopy(master)

    # skills: matched items first inside a category, categories with most matches first
    cats = []
    for idx, cat in enumerate(r["skills"]):
        items = sorted(enumerate(cat["items"]), key=lambda p: (-_score(p[1]["tags"], wanted), p[0]))
        cat["items"] = [it for _, it in items]
        cat["_score"] = sum(_score(i["tags"], wanted) for i in cat["items"])
        cats.append((idx, cat))
    cats.sort(key=lambda p: (-p[1]["_score"], p[0]))
    r["skills"] = [c for _, c in cats]

    # experience: jobs keep chronological order; bullets inside each job are re-ranked
    for job in r["experience"]:
        job["bullets"] = [b for _, b in sorted(enumerate(job["bullets"]),
                                               key=lambda p: (-_score(p[1]["tags"], wanted), p[0]))]
        for extra in job.get("extra_sections", []):
            extra["bullets"] = [b for _, b in sorted(enumerate(extra["bullets"]),
                                                     key=lambda p: (-_score(p[1]["tags"], wanted), p[0]))]

    # projects: most relevant first, bullets re-ranked
    projs = sorted(enumerate(r["projects"]), key=lambda p: (-_score(p[1]["tags"], wanted), p[0]))
    r["projects"] = [p for _, p in projs]
    for p in r["projects"]:
        p["bullets"] = [b for _, b in sorted(enumerate(p["bullets"]),
                                             key=lambda q: (-_score(q[1]["tags"], wanted), q[0]))]

    # summary: lead + matched skills + top-3 relevant master highlights + master tail (all master content)
    hl = [h for _, h in sorted(enumerate(r["summary_highlights"]),
                               key=lambda p: (-_score(p[1]["tags"], wanted), p[0]))][:3]
    matched = matched_skill_names(master, analysis)
    summary = r["summary_lead"]
    if matched:
        summary += f" with hands-on experience in {', '.join(matched[:6])}"
    summary += ". Notable work: " + "; ".join(h["text"] for h in hl) + ". " + r["summary_tail"]
    r["summary"] = summary
    r["matched_skills"] = matched
    return r


def matched_skill_names(master: dict, analysis: JobAnalysis) -> list[str]:
    """Master skills (verbatim names, parenthetical detail dropped) that the job asks for.

    One entry per requested tag, in the order the job mentions them; only skills that
    exist in the master resume can ever appear here.
    """
    out: list[str] = []
    for tag in analysis.tags:
        for cat in master["skills"]:
            hit = next((i for i in cat["items"] if tag in i["tags"]), None)
            if hit:
                name = re.sub(r"\s*\(.*?\)", "", hit["text"]).strip()
                if name not in out:
                    out.append(name)
                break
    return out


# ---------------------------------------------------------------- filenames
def _slug(s: str, limit: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")
    return s[:limit].strip("_")


_NO_COMPANY = {"", "not available", "n/a", "na", "none", "unknown", "company"}


def resume_filename(candidate_name: str, company: str) -> str:
    """Abhishek_Chavan_3_5_years_<Company>.pdf, or ..._Ai_Automation.pdf when the post names no company."""
    who = _slug(candidate_name, 40) or "Resume"
    comp = (company or "").strip()
    tail = "AI_Automation" if comp.lower() in _NO_COMPANY else (_slug(comp) or "AI_Automation")
    return f"{who}_3_5_years_{tail}.pdf"


def resume_path_for(out_dir: Path, candidate_name: str, company: str, job_id: str) -> Path:
    """resumes/<job_id>/<clean file name>.pdf - one folder per job, so the file name the recruiter
    sees is always clean and two jobs can never overwrite each other's resume."""
    return out_dir / job_id / resume_filename(candidate_name, company)


def _pdf_job_id(path: Path) -> str | None:
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return (pdf.metadata or {}).get("Keywords")
    except Exception:
        return None


def pdf_job_id(path: str | Path) -> str | None:
    """Job id embedded in a generated resume (used to verify resume<->job match before sending)."""
    return _pdf_job_id(Path(path))


# ---------------------------------------------------------------- rendering
def _styles() -> dict[str, ParagraphStyle]:
    base = dict(fontName="Helvetica", fontSize=9.6, leading=12.2, alignment=TA_LEFT)
    return {
        "name": ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=17, leading=20),
        "head": ParagraphStyle("head", fontName="Helvetica", fontSize=9.6, leading=12, spaceAfter=1),
        "sec": ParagraphStyle("sec", fontName="Helvetica-Bold", fontSize=10.5, leading=13, spaceBefore=7, spaceAfter=1),
        "body": ParagraphStyle("body", **base),
        "job": ParagraphStyle("job", fontName="Helvetica-Bold", fontSize=9.8, leading=12.4, spaceBefore=4),
        "sub": ParagraphStyle("sub", fontName="Helvetica-Oblique", fontSize=9.4, leading=12),
        "bullet": ParagraphStyle("bullet", leftIndent=11, bulletIndent=2, **base),
    }


def render_pdf(resume: dict, out_path: str | Path, job_id: str, job_title: str = "") -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    s = _styles()
    c = resume["contact"]
    E = escape
    doc = SimpleDocTemplate(
        str(out), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=13 * mm, bottomMargin=13 * mm,
        title=f"{resume['name']} - Resume", author=resume["name"],
        subject=f"Resume tailored for: {job_title}"[:120], keywords=job_id,
    )
    f: list = [Paragraph(E(resume["name"].upper()), s["name"]), Paragraph(E(resume["headline"].upper()), s["head"]),
               Paragraph(E(f"{c['phone']} | {c['email']} | {c['location']}"), s["head"]),
               Paragraph(E(f"{c['linkedin']} | GitHub: {c['github']}"), s["head"])]

    def section(title: str) -> None:
        f.append(Paragraph(title, s["sec"]))
        f.append(HRFlowable(width="100%", thickness=0.6, color="#000000", spaceAfter=3))

    section("SUMMARY")
    f.append(Paragraph(E(resume["summary"]), s["body"]))

    section("SKILLS")
    for cat in resume["skills"]:
        f.append(Paragraph(f"<b>{E(cat['category'])}:</b> {E(', '.join(i['text'] for i in cat['items']))}", s["body"]))

    section("EXPERIENCE")
    for job in resume["experience"]:
        block = [Paragraph(f"{E(job['title'])} — {E(job['dates'])}", s["job"]),
                 Paragraph(f"{E(job['company'])}, {E(job['location'])}", s["sub"])]
        if job.get("subheading"):
            block.append(Paragraph(E(job["subheading"]), s["sub"]))
        f.append(KeepTogether(block + [Paragraph(E(job["bullets"][0]["text"]), s["bullet"], bulletText="-")]))
        for b in job["bullets"][1:]:
            f.append(Paragraph(E(b["text"]), s["bullet"], bulletText="-"))
        for extra in job.get("extra_sections", []):
            f.append(Paragraph(E(extra["subheading"]), s["sub"]))
            for b in extra["bullets"]:
                f.append(Paragraph(E(b["text"]), s["bullet"], bulletText="-"))
        if job.get("technologies"):
            f.append(Paragraph(f"<b>Technologies:</b> {E(job['technologies'])}", s["body"]))

    section("AI / GENAI PROJECTS")
    for p in resume["projects"]:
        f.append(Paragraph(f"<b>{E(p['title'])}</b> — {E(p['stack'])}", s["body"]))
        for b in p["bullets"]:
            f.append(Paragraph(E(b["text"]), s["bullet"], bulletText="-"))

    section("EDUCATION")
    for ed in resume["education"]:
        f.append(Paragraph(f"<b>{E(ed['school'])}</b> — {E(ed['dates'])}", s["body"]))
        f.append(Paragraph(E(ed["degree"]), s["body"]))
    f.append(Spacer(1, 1))
    try:
        doc.build(f)
    except Exception as exc:  # reportlab raises many types
        raise ResumeError(f"PDF generation failed: {exc}") from exc
    return out


@dataclass
class BuiltResume:
    path: Path
    matched_skills: list[str]
    summary: str


def build_resume(master: dict, analysis: JobAnalysis, job_title: str, company: str, out_dir: str | Path) -> BuiltResume:
    tailored = tailor(master, analysis)
    path = resume_path_for(Path(out_dir), master.get("name", ""), company, analysis.job_id)
    render_pdf(tailored, path, analysis.job_id, job_title)
    return BuiltResume(path=path, matched_skills=tailored["matched_skills"], summary=tailored["summary"])