"""Job-post analysis: job id, recruiter email extraction, skills, experience, instructions.
Everything here is derived strictly from the text of the post. Nothing is guessed:
an e-mail address is only returned if it is literally present in the text
(including simple "name [at] domain [dot] com" obfuscation, which is still the
recruiter's own published address).
"""
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from .validators import EMAIL_RE

NOT_AVAILABLE = "Not Available"

# ---------------------------------------------------------------- job identity
_ACTIVITY_RE = re.compile(r"(?:activity|ugcPost|share)[:\-]?(\d{15,})", re.IGNORECASE)


def normalize_post_url(url: str) -> str:
    """Stable form of a LinkedIn post URL: lower-case host, no query/fragment/trailing slash."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    return f"{parts.scheme.lower() or 'https'}://{host}{parts.path.rstrip('/')}"


def job_id_for(post_url: str, company: str = "", title: str = "", text: str = "") -> str:
    """Stable job identifier: LinkedIn activity id if present, else hash of normalized URL.

    Falls back to a hash of company+title+text when the row has no URL at all.
    """
    norm = normalize_post_url(post_url)
    m = _ACTIVITY_RE.search(post_url or "")
    if m:
        return f"li-{m.group(1)}"
    basis = norm or f"{company}|{title}|{text[:500]}".lower()
    return "job-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- e-mail extraction
_OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*[\[\(\{]\s*(?:at|@)\s*[\]\)\}]\s*"
    r"([A-Za-z0-9\-]+(?:(?:\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*|\s+dot\s+|\.)[A-Za-z0-9\-]+)+)",
    re.IGNORECASE,
)
_BAD_LOCALPARTS = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster"}
_BAD_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "pdf", "docx", "xlsx"}


def _clean_candidate(addr: str) -> str | None:
    addr = addr.strip().strip(".,;:()<>[]\"'").lower()
    if not EMAIL_RE.fullmatch(addr):
        return None
    local, _, domain = addr.rpartition("@")
    if local in _BAD_LOCALPARTS or domain.rsplit(".", 1)[-1] in _BAD_TLDS:
        return None
    if ".." in addr or domain.startswith(("-", ".")):
        return None
    return addr


def extract_emails(text: str) -> list[str]:
    """All distinct valid e-mail addresses literally present in the text, in order."""
    text = text or ""
    found: list[str] = []
    for m in EMAIL_RE.finditer(text):
        c = _clean_candidate(m.group(0))
        if c and c not in found:
            found.append(c)
    for m in _OBFUSCATED_RE.finditer(text):
        domain = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*|\s+dot\s+", ".", m.group(2), flags=re.IGNORECASE)
        domain = re.sub(r"\s+", "", domain)
        c = _clean_candidate(f"{m.group(1)}@{domain}")
        if c and c not in found:
            found.append(c)
    return found


def pick_recruiter_email(text: str, column_value: str = "") -> tuple[str, str]:
    """(email or NOT_AVAILABLE, source) preferring the scraper's Email column, then post text."""
    for source, blob in (("email column", column_value), ("post text", text)):
        for cand in extract_emails(blob or ""):
            return cand, source
    return NOT_AVAILABLE, "none"


# ---------------------------------------------------------------- skills
# canonical tag -> (display name, regex). Tags match tags in master_resume.json.
SKILL_VOCAB: dict[str, tuple[str, str]] = {
    "python": ("Python", r"\bpython\b"),
    "fastapi": ("FastAPI", r"\bfast\s?api\b"),
    "django": ("Django", r"\bdjango\b"),
    "flask": ("Flask", r"\bflask\b"),
    "rest": ("REST APIs", r"\brest(?:ful)?(?:\s?apis?)?\b|\bapi development\b|\bapis?\b"),
    "microservices": ("Microservices", r"\bmicro-?services?\b"),
    "postgresql": ("PostgreSQL", r"\bpostgres(?:ql)?\b"),
    "mysql": ("MySQL", r"\bmysql\b"),
    "mongodb": ("MongoDB", r"\bmongo(?:db)?\b"),
    "sql": ("SQL", r"\bsql\b"),
    "sqlalchemy": ("SQLAlchemy", r"\bsqlalchemy\b"),
    "redis": ("Redis", r"\bredis\b"),
    "kafka": ("Kafka", r"\bkafka\b"),
    "docker": ("Docker", r"\bdocker\b"),
    "kubernetes": ("Kubernetes", r"\bkubernetes\b|\bk8s\b"),
    "aws": ("AWS", r"\baws\b|\bamazon web services\b|\blambda\b"),
    "azure": ("Azure", r"\bazure\b"),
    "gcp": ("GCP", r"\bgcp\b|\bgoogle cloud\b"),
    "cicd": ("CI/CD", r"\bci\s?/\s?cd\b|\bcicd\b|\bjenkins\b|\bgithub actions\b"),
    "linux": ("Linux", r"\blinux\b"),
    "git": ("Git", r"\bgit\b"),
    "jira": ("JIRA", r"\bjira\b"),
    "selenium": ("Selenium", r"\bselenium\b"),
    "pytest": ("PyTest", r"\bpytest\b"),
    "testing": ("Testing", r"\bunit test\w*\b|\bmock(?:ing)?\b|\btest automation\b"),
    "postman": ("Postman", r"\bpostman\b"),
    "automation": ("Automation", r"\bautomation\b|\bautomate\w*\b"),
    "pandas": ("Pandas", r"\bpandas\b"),
    "numpy": ("NumPy", r"\bnumpy\b"),
    "rag": ("RAG", r"\brag\b|retrieval[- ]augmented"),
    "llm": ("LLMs", r"\bllms?\b|large language models?"),
    "genai": ("GenAI", r"\bgen\s?ai\b|\bgenerative ai\b"),
    "openai": ("OpenAI APIs", r"\bopenai\b"),
    "langchain": ("LangChain", r"\blangchain\b"),
    "langgraph": ("LangGraph", r"\blanggraph\b"),
    "chromadb": ("ChromaDB", r"\bchroma(?:db)?\b"),
    "vector": ("Vector Search", r"\bvector (?:db|database|search|store)s?\b|\bembeddings?\b"),
    "agentic": ("Agentic AI", r"\bagentic\b|\bai agents?\b"),
    "prompt": ("Prompt Engineering", r"\bprompt engineering\b"),
    "nlp": ("NLP", r"\bnlp\b|natural language processing"),
    "pydantic": ("Pydantic", r"\bpydantic\b"),
    "openapi": ("OpenAPI", r"\bopenapi\b|\bswagger\b"),
    "oop": ("OOP", r"\boop\b|object[- ]oriented"),
    "etl": ("ETL / data pipelines", r"\betl\b|data pipelines?"),
    "async": ("Async", r"\basync\w*\b|\basyncio\b"),
    "agile": ("Agile", r"\bagile\b|\bscrum\b"),
    "frontend": ("HTML/CSS", r"\bhtml\b|\bcss\b|\bbootstrap\b"),
    "celery": ("Celery", r"\bcelery\b"),
    "java": ("Java", r"\bjava\b"),
    "nodejs": ("Node.js", r"\bnode\.?js\b"),
    "react": ("React", r"\breact(?:\.?js)?\b"),
}
_COMPILED = {k: (name, re.compile(rx, re.IGNORECASE)) for k, (name, rx) in SKILL_VOCAB.items()}

_EXP_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b(?:\s+of)?(?:\s+(?:relevant\s+)?experience)?",
    re.IGNORECASE,
)
_INSTR_RE = re.compile(
    r"(send|share|email|mail|forward|apply|dm|whatsapp|drop|submit)[^.\n]{0,80}"
    r"(cv|resume|profile|application|e-?mail|@|link|dm|whatsapp)",
    re.IGNORECASE,
)


@dataclass
class JobAnalysis:
    job_id: str
    tags: list[str] = field(default_factory=list)          # canonical tags found in the post
    skill_names: list[str] = field(default_factory=list)   # display names, same order
    experience: str = NOT_AVAILABLE
    instructions: str = NOT_AVAILABLE
    complete: bool = True
    problem: str = ""


def extract_skills(text: str) -> tuple[list[str], list[str]]:
    tags, names = [], []
    for tag, (name, rx) in _COMPILED.items():
        if rx.search(text or ""):
            tags.append(tag)
            names.append(name)
    return tags, names


def extract_experience(text: str) -> str:
    for m in _EXP_RE.finditer(text or ""):
        lo, hi = int(m.group(1)), m.group(2)
        if lo > 40 or (hi and int(hi) > 40):
            continue
        return f"{lo}-{hi} years" if hi else f"{lo}+ years"
    return NOT_AVAILABLE


def extract_instructions(text: str) -> str:
    for line in re.split(r"\n+", text or ""):
        if _INSTR_RE.search(line):
            return re.sub(r"\s+", " ", line).strip()[:300]
    return NOT_AVAILABLE


def analyze_job(post_url: str, title: str, company: str, text: str, min_chars: int = 40) -> JobAnalysis:
    text = text or ""
    tags, names = extract_skills(f"{title}\n{text}")
    a = JobAnalysis(
        job_id=job_id_for(post_url, company, title, text),
        tags=tags,
        skill_names=names,
        experience=extract_experience(text),
        instructions=extract_instructions(text),
    )
    if len(text.strip()) < min_chars and len(tags) < 2:
        a.complete = False
        a.problem = "Job description missing or too short to tailor a resume"
    return a