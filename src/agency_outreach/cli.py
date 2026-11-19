from __future__ import annotations

import argparse
import csv
import logging
import logging.handlers
import os
import random
import sys
import time
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

from . import applications
from .config import ConfigError, Settings
from .email_sender import SendError, SmtpMailer, build_message
from .excel_parser import ParseError, parse_leads
from .models import Lead
from .resume_builder import ResumeError
from .state import StateStore
from .templating import render
from .tracker import Tracker, TrackerError
from .whatsapp import build_link, render_queue_html

log = logging.getLogger("agency_outreach")
DEFAULT_TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


def setup_logging(verbose: bool) -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logfile = logging.handlers.RotatingFileHandler("logs/outreach.log", maxBytes=1_000_000, backupCount=3)
    logfile.setFormatter(fmt)
    root.handlers[:] = [console, logfile]


def select_email_targets(leads: list[Lead], include_non_it: bool = True) -> list[Lead]:
    """Leads with a valid email, de-duplicated by address."""
    seen: set[str] = set()
    out: list[Lead] = []
    for lead in leads:
        if not lead.email or lead.email in seen:
            continue
        seen.add(lead.email)
        out.append(lead)
    return out


def cmd_parse(args: argparse.Namespace) -> int:
    leads = parse_leads(args.excel)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "contacts.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "author", "company", "job_title", "email", "whatsapp", "post_url"])
        for l in leads:
            w.writerow([l.rank, l.name, l.company, l.job_title, l.email or "", l.whatsapp or "", l.post_url])
    with (out / "manual_followup.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "author", "company", "post_url", "why"])
        for l in leads:
            if not l.email and not l.whatsapp:
                w.writerow([l.rank, l.name, l.company, l.post_url, "No direct contact details - connect on LinkedIn"])
    print(f"{len(leads)} leads -> {out}/contacts.csv, manual_followup.csv")
    return 0


def cmd_email(args: argparse.Namespace) -> int:
    cfg = Settings.load(args.env, require_smtp=args.send)
    store = StateStore(cfg.state_db)
    targets = [l for l in select_email_targets(parse_leads(args.excel))
               if not store.already_sent(l.email or "")]
    cap = min(args.limit or cfg.max_per_run, cfg.max_per_run)
    targets = targets[:cap]
    if not targets:
        print("Nothing to send (all done or no valid targets).")
        return 0
    template_dir = Path(args.templates)
    mode = "SENDING" if args.send else "DRY-RUN (no email sent; add --send to send)"
    print(f"{mode}: {len(targets)} recipient(s)")

    if not args.send:
        for l in targets:
            build_message(l, cfg, template_dir)  # validates templates + attachment
            print(f"  would send -> {l.display_name} <{l.email}> ({l.company or 'N/A'})")
        return 0

    sent = failed = 0
    try:
        with SmtpMailer(cfg) as mailer:
            for i, l in enumerate(targets, 1):
                try:
                    mailer.send(build_message(l, cfg, template_dir))
                    store.record(l.email, l.display_name, "sent")
                    sent += 1
                    log.info("[%d/%d] sent -> %s <%s>", i, len(targets), l.display_name, l.email)
                except SendError as exc:
                    store.record(l.email, l.display_name, "failed", str(exc))
                    failed += 1
                    log.error("[%d/%d] FAILED %s <%s>: %s", i, len(targets), l.display_name, l.email, exc)
                    if "authentication" in str(exc).lower():
                        break
                if i < len(targets):
                    time.sleep(random.uniform(cfg.delay_min, cfg.delay_max))
    except KeyboardInterrupt:
        print("\nInterrupted - progress is saved; re-run to continue.")
    print(f"Done: {sent} sent, {failed} failed.")
    return 0 if failed == 0 else 1


def cmd_whatsapp(args: argparse.Namespace) -> int:
    cfg = Settings.load(args.env)
    leads = [l for l in parse_leads(args.excel) if l.whatsapp]
    template_dir = Path(args.templates)
    if not args.open:
        n = render_queue_html(leads, cfg, template_dir, Path(args.out))
        print(f"{n} WhatsApp chats -> {args.out} (open in a browser, or use --open for guided mode)")
        return 0

    store = StateStore(cfg.state_db)
    todo = [l for l in leads if not store.wa_done(l.whatsapp or "")]
    print(f"Guided mode: {len(todo)} chat(s). For each: WhatsApp opens with the message pre-filled -> press Send -> "
          "come back and press Enter (s = skip, q = quit).")
    for i, l in enumerate(todo, 1):
        text = render(template_dir / "whatsapp.txt", l, cfg)
        print(f"\n[{i}/{len(todo)}] {l.display_name} (+{l.whatsapp})")
        webbrowser.open(build_link(l.whatsapp or "", text))
        answer = input("Sent? [Enter=yes, s=skip, q=quit]: ").strip().lower()
        if answer == "q":
            break
        if answer != "s":
            store.wa_record(l.whatsapp or "", l.display_name)
    print("Progress saved; re-run to continue where you stopped.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    cfg = Settings.load(args.env)
    for email, agency, status, at in StateStore(cfg.state_db).summary():
        print(f"{at}  {status:6}  {agency:<28} {email}")
    return 0


def _settings(args: argparse.Namespace, **kw) -> Settings:
    """Settings.load plus --skip-domain overrides (adds to SKIP_EMAIL_DOMAINS)."""
    from dataclasses import replace
    cfg = Settings.load(args.env, **kw)
    extra = tuple(d.strip().lower().lstrip("@") for d in (getattr(args, "skip_domain", None) or []))
    return replace(cfg, skip_domains=tuple(dict.fromkeys(cfg.skip_domains + extra))) if extra else cfg


def _tracker(args: argparse.Namespace, cfg: Settings) -> Tracker:
    return Tracker(args.tracker or cfg.tracker_path)


def cmd_apply_prepare(args: argparse.Namespace) -> int:
    """Leads -> tracker rows + tailored resume PDFs + e-mail drafts. Never sends anything."""
    cfg = Settings.load(args.env)
    tracker = _tracker(args, cfg)
    stats = applications.prepare(parse_leads(args.excel), tracker, StateStore(cfg.state_db), cfg, args.limit)
    print("prepare:", ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "nothing to do")
    print(f"tracker: {args.tracker or cfg.tracker_path}   resumes: {cfg.resume_dir}/")
    mode = "AUTO_SEND_EMAILS=true: drafts are pre-approved" if cfg.auto_send else \
        "Drafts await your approval: apply-review, then apply-approve"
    print(mode)
    return 0


def cmd_apply_review(args: argparse.Namespace) -> int:
    cfg = _settings(args)
    rows = applications.pending_review(_tracker(args, cfg), StateStore(cfg.state_db), cfg)
    if not rows:
        print("No drafts awaiting approval.")
        return 0
    if args.summary:
        print(f"{'#':>3}  {'Job ID':<19} {'To':<34} {'Company':<28} {'Title':<30} Exp.asked")
        for i, r in enumerate(rows, 1):
            print(f"{i:>3}  {r['Job ID']:<19} {r['Recruiter Email'][:33]:<34} {r['Company Name'][:27]:<28} "
                  f"{r['Job Title'][:29]:<30} {r['Experience Required']}")
        print(f"\n{len(rows)} unique-recipient draft(s) awaiting approval (duplicates already removed).")
        return 0
    for r in rows[: args.limit or len(rows)]:
        print("=" * 78)
        print(f"Job ID     : {r['Job ID']}\nTo         : {r['Recruiter Email']}\n"
              f"Job        : {r['Job Title']} @ {r['Company Name']}  (experience asked: {r['Experience Required']})\nPost       : {r['LinkedIn Post URL']}\n"
              f"Attachment : {r['Customized Resume Path']}\nSubject    : {r['Email Subject']}\n\n{r['Email Body or Template']}")
    print("=" * 78)
    print(f"{len(rows)} draft(s) awaiting approval. Approve with: agency-outreach apply-approve --job-id ID [--job-id ID] | --all")
    return 0


def cmd_apply_approve(args: argparse.Namespace) -> int:
    if not args.all and not args.job_id:
        print("Specify --job-id ID (repeatable) or --all. You can also set 'Email Approval Status' to Approved in the tracker.")
        return 2
    cfg = _settings(args)
    n = applications.approve(_tracker(args, cfg), args.job_id, args.all, StateStore(cfg.state_db), cfg)
    print(f"{n} draft(s) approved. Nothing is sent until: agency-outreach apply-send --send")
    return 0


def cmd_apply_send(args: argparse.Namespace) -> int:
    cfg = _settings(args, require_smtp=args.send)
    tracker = _tracker(args, cfg)
    print("SENDING approved drafts" if args.send else "DRY-RUN (nothing sent; add --send to send approved drafts)")
    stats = applications.send(tracker, StateStore(cfg.state_db), cfg, really_send=args.send,
                              limit=args.limit, retry_failed=args.retry_failed, send_all=args.all)
    print("send:", ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "nothing eligible")
    return 1 if stats.get("failed") else 0


def cmd_apply_followup(args: argparse.Namespace) -> int:
    cfg = _settings(args, require_smtp=args.send)
    days = args.days if args.days is not None else cfg.followup_after_days
    print(f"{'SENDING' if args.send else 'DRY-RUN (nothing sent; add --send)'} follow-ups to applications sent {days}+ days ago")
    stats = applications.followup(_tracker(args, cfg), StateStore(cfg.state_db), cfg, days=days,
                                  really_send=args.send, limit=args.limit, send_all=args.all)
    print("followup:", ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "nothing eligible")
    return 1 if stats.get("failed") else 0


def cmd_apply_status(args: argparse.Namespace) -> int:
    from collections import Counter
    cfg = Settings.load(args.env)
    t = _tracker(args, cfg)
    c: Counter = Counter()
    for jid in t.job_ids():
        r = t.get(jid)
        c[(r["Resume Generation Status"], r["Email Approval Status"], r["Email Sending Status"])] += 1
    print(f"{len(t)} job(s) in tracker")
    for (res, appr, snd), n in sorted(c.items(), key=lambda kv: -kv[1]):
        print(f"{n:5}  resume={res or '-':<17} approval={appr or '-':<17} send={snd or '-'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument("--env", default=".env", help="path to .env file")
    common.add_argument("--excel", default=None, help="path to the leads workbook (default: LEADS_EXCEL from .env)")
    common.add_argument("--pdf", dest="excel", help="alias for --excel")
    common.add_argument("--skip-domain", action="append", metavar="DOMAIN",
                        help="never e-mail this domain, e.g. --skip-domain gmail.com (repeatable; also SKIP_EMAIL_DOMAINS)")
    common.add_argument("--tracker", default=None, help="job tracker workbook (default: TRACKER_PATH)")
    common.add_argument("--templates", default=str(DEFAULT_TEMPLATES))

    p = argparse.ArgumentParser(prog="agency-outreach", parents=[common], description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("parse", parents=[common], help="extract contacts to CSV")
    s.add_argument("--out", default="data")
    s.set_defaults(func=cmd_parse)

    s = sub.add_parser("email", parents=[common], help="send resume emails (dry-run unless --send)")
    s.add_argument("--send", action="store_true", help="actually send")
    s.add_argument("--limit", type=int, help="max recipients this run")
    s.add_argument("--include-non-it", action="store_true")
    s.set_defaults(func=cmd_email)

    s = sub.add_parser("whatsapp", parents=[common], help="build wa.me click-to-chat queue")
    s.add_argument("--out", default="data/whatsapp_queue.html")
    s.add_argument("--include-non-it", action="store_true")
    s.add_argument("--open", action="store_true", help="guided mode: open each chat in turn")
    s.set_defaults(func=cmd_whatsapp)

    s = sub.add_parser("apply-prepare", parents=[common], help="LinkedIn leads -> tailored resume + e-mail draft (sends nothing)")
    s.add_argument("--limit", type=int, help="process at most N new jobs this run")
    s.set_defaults(func=cmd_apply_prepare)

    s = sub.add_parser("apply-review", parents=[common], help="show drafts awaiting approval")
    s.add_argument("--limit", type=int)
    s.add_argument("--summary", action="store_true", help="one line per draft instead of full text")
    s.set_defaults(func=cmd_apply_review)

    s = sub.add_parser("apply-approve", parents=[common], help="approve drafts for sending")
    s.add_argument("--job-id", action="append", help="job id to approve (repeatable)")
    s.add_argument("--all", action="store_true", help="approve every draft awaiting approval")
    s.set_defaults(func=cmd_apply_approve)

    s = sub.add_parser("apply-send", parents=[common], help="send APPROVED drafts (dry-run unless --send)")
    s.add_argument("--send", action="store_true", help="actually send")
    s.add_argument("--limit", type=int)
    s.add_argument("--retry-failed", action="store_true", help="also retry rows with status Failed")
    s.add_argument("--all", action="store_true", help="ignore the MAX_PER_RUN cap: send every approved draft this run")
    s.set_defaults(func=cmd_apply_send)

    s = sub.add_parser("apply-followup", parents=[common],
                       help="one short follow-up to recruiters e-mailed N+ days ago (dry-run unless --send)")
    s.add_argument("--days", type=int, help="minimum days since the first e-mail (default FOLLOWUP_AFTER_DAYS=14)")
    s.add_argument("--send", action="store_true", help="actually send")
    s.add_argument("--limit", type=int)
    s.add_argument("--all", action="store_true", help="ignore the MAX_PER_RUN cap")
    s.set_defaults(func=cmd_apply_followup)

    s = sub.add_parser("apply-status", parents=[common], help="tracker status summary")
    s.set_defaults(func=cmd_apply_status)

    s = sub.add_parser("report", parents=[common], help="show send history")
    s.set_defaults(func=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    if not args.excel:
        load_dotenv(args.env)
        args.excel = os.getenv("LEADS_EXCEL", "").strip() or None
    try:
        return args.func(args)
    except (ConfigError, ParseError, SendError, ResumeError, TrackerError) as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())