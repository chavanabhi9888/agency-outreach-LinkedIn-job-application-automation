# agency-outreach

Two workflows in one tool:

1. **Agency outreach** - reads a leads workbook / agency list and (1) emails your resume individually and idempotently, (2) builds a one-tap WhatsApp queue, (3) lists contacts that only accept web forms.
2. **LinkedIn job applications (`apply-*`)** - for each scraped LinkedIn job post: tailored resume PDF + personalised email draft + approval + guarded send + Excel tracking. See the section at the bottom.

## Why WhatsApp is semi-automatic (design decision)
Fully automated WhatsApp sending to strangers is not done here on purpose:
- The official WhatsApp Business Cloud API only allows pre-approved templates to users who **opted in**; cold outreach to agencies violates its policy.
- Browser/`pywhatkit`/Selenium automation of a personal account violates WhatsApp's Terms and commonly gets the number **banned** - risking your primary number during a job search.

So the tool generates `wa.me` click-to-chat links with a pre-filled message. A link cannot attach a file, so set `RESUME_URL` (a Google Drive / GitHub share link to your resume) and the message includes it; you only press Send (about 15 chats, ~3 minutes). Guided mode (`whatsapp --open`) remembers which chats you finished.

## What the PDF actually contains (measured on your file)
| Metric | Count |
|---|---|
| Agencies parsed | 50 |
| With a direct email | 20 (all IT-relevant) |
| WhatsApp-capable mobile numbers | 15 |
| IT-relevant but contact-form only | 28 (see `data/manual_followup.csv`) |

Landlines and toll-free numbers are excluded from WhatsApp automatically. The PDF itself says details must be verified before use; expect some bounces.

## Architecture
```
PDF --pdfplumber--> rows --> Agency objects (validators: email, mobile, IT flag)
                                  |
      +---------------------------+-----------------------------+
      v                           v                             v
  parse -> CSVs          email -> SMTP + SQLite log     whatsapp -> HTML link queue
```
| Module | Responsibility |
|---|---|
| `pdf_parser.py` | Table extraction; pure `rows_to_agencies()` is unit-tested |
| `validators.py` | Email regex, Indian-mobile normalisation, IT-coverage flag |
| `email_sender.py` | Message building, SMTP (STARTTLS/SSL), retry with reconnect, permanent-vs-transient errors |
| `state.py` | SQLite log: never emails the same address twice, safe to re-run/resume |
| `whatsapp.py` | `wa.me` link queue |
| `config.py` | `.env` loading and validation |
| `cli.py` | Commands, logging, rate limiting |
| `job_analysis.py` | Job id, recruiter-email extraction, skills / experience / instructions from a post |
| `resume_builder.py` | Tailors `data/master_resume.json` (re-order/select only) and renders the ATS PDF |
| `drafts.py` | Per-job email subject and body |
| `tracker.py` | Excel tracker, atomic saves |
| `applications.py` | prepare -> duplicate sweep -> approve -> send pipeline |

## Setup
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt && python -m pip install -e .   # always 'python -m pip' so it installs into the active venv
cp .env.example .env                                   # then edit
```
Place the agency PDF and your resume next to the project (or set `RESUME_PATH`, `--pdf`). Copy `data/master_resume.example.json` to `data/master_resume.json` and fill in your own resume (the real one is git-ignored).

### Gmail app password
Google Account -> Security -> enable 2-Step Verification -> App passwords -> create one for "Mail" -> paste as `SMTP_PASSWORD`. Never use your normal password; never commit `.env` (it is git-ignored).

## Usage
```bash
agency-outreach parse                      # writes data/contacts.csv + data/manual_followup.csv
agency-outreach email                      # DRY RUN: validates templates + attachment, sends nothing
agency-outreach email --limit 3 --send     # send a small first batch to check deliverability
agency-outreach email --send               # send the rest (skips already-sent addresses)
agency-outreach whatsapp --open            # guided mode: opens each chat pre-filled; you press Send, then Enter
agency-outreach whatsapp                   # or: writes data/whatsapp_queue.html link list
agency-outreach report                     # send history
```
`python -m agency_outreach ...` works without installing (set `PYTHONPATH=src`; PowerShell: `$env:PYTHONPATH="src"`). Python 3.10+ is supported (tested on 3.10, 3.11, 3.13). Add `-v` for debug logs. Logs: `logs/outreach.log` (rotating).

## Safety behaviour
- **Dry-run by default**; sending needs an explicit `--send`.
- One message per recipient - no CC/BCC leaks between agencies.
- 20-45 s randomised delay between sends; `MAX_PER_RUN` cap (default 30).
- Failures recorded and retried on the next run; authentication errors abort the batch; 5xx recipient errors are not retried.
- Ctrl+C is safe: progress is saved per email.
- Attachment must exist and be under 5 MB.

## Configuration (`.env`)
| Variable | Purpose |
|---|---|
| `SMTP_HOST/PORT/SECURITY/USER/PASSWORD` | SMTP server; `SECURITY=starttls` (587) or `ssl` (465) |
| `SENDER_NAME`, `SENDER_EMAIL`, `PHONE`, `CITY`, `LINKEDIN_URL`, `GITHUB_URL` | Filled into templates |
| `RESUME_PATH` | PDF to attach to emails |
| `LEADS_EXCEL` | Default leads workbook for `--excel` |
| `RESUME_URL` | Public link to the resume, included in WhatsApp text |
| `STATE_DB` | SQLite idempotency log |
| `DELAY_MIN_SECONDS`, `DELAY_MAX_SECONDS`, `MAX_PER_RUN` | Rate limiting |

Edit wording in `templates/*.txt` (`$agency_name`, `$sender_name`, `$phone`, `$city`, `$linkedin`, `$github`). An unknown placeholder raises an error instead of sending a broken email.

## Good practice for the outreach itself
- Send a first batch of 3, check the Sent folder and any bounces, then continue.
- Never pay an agency for interviews or placement. Verify roles on the client's own site.
- Follow up once (about 5 days later) by replying to your own sent mail; don't re-run `--send` to re-email.
- Honour any "remove me" reply: delete that row from your list or use `--pdf` on an edited copy.
- Submit the 28 contact-form agencies manually, prioritising IT ones such as Randstad, Adecco, Michael Page, Quess, Collabera.

## Testing
```bash
pytest -q        # 50 tests (+1 skipped): validators, state, message building, and the whole apply-* workflow
```
CI-friendly; the real-PDF test auto-skips if the sample file is absent.

## Troubleshooting
| Symptom | Fix |
|---|---|
| `SMTP authentication failed` / Google error 534 `5.7.9 Application-specific password required` | Your normal Gmail password is rejected. Enable 2-Step Verification, create an App password at myaccount.google.com/apppasswords, paste it into `SMTP_PASSWORD` (spaces optional), and keep `SMTP_USER` equal to the Gmail address |
| `No agency rows found` | PDF layout changed; check `rows_to_agencies` against `pdfplumber` output |
| Emails land in spam | Small first batch, no link-heavy body, plain wording; reply threads help |
| `Missing required setting` | Fill the named variable in `.env` |

## Extending
Add a new channel by writing a module that takes `Agency` + `Settings` and reuse `StateStore`; add a Naukri/LinkedIn queue the same way as `whatsapp.py`.

---

# LinkedIn job-application workflow (`apply-*` commands)

Turns each row of the LinkedIn leads workbook (`--excel`, columns *Post URL, Company, Job Title, Location, Email, Post Text*) into: a job-specific resume PDF, a personalised e-mail draft, an approval step, a guarded send, and a tracked Excel row. The legacy `email` / `whatsapp` / `parse` / `report` commands are unchanged.

```
leads xlsx --> apply-prepare --> tracker xlsx + resumes/<job_id>/*.pdf + drafts   (sends nothing)
                                      |
                      apply-review / apply-approve  (or set "Email Approval Status" = Approved in Excel)
                                      |
                   apply-send  (dry-run)   -->  apply-send --send   (only Approved, validated, de-duplicated rows)
```

## Commands
```bash
agency-outreach apply-prepare --excel leads.xlsx --limit 30   # resume + draft per job (limit counts rows, incl. rows without email); idempotent, resumable
agency-outreach apply-review --summary                         # one line per draft (all of them); omit --summary for full text
agency-outreach apply-approve --job-id li-7123...  |  --all    # explicit approval (duplicates are never approved)
agency-outreach apply-send                                     # DRY RUN: shows what would be sent
agency-outreach apply-send --send                              # really send approved drafts, up to MAX_PER_RUN per run
agency-outreach apply-send --send --all --skip-domain gmail.com   # send ALL approved drafts now (no cap), never to gmail.com
agency-outreach apply-send --send --retry-failed               # also retry rows marked Failed
agency-outreach apply-status                                   # counts per status
```

## Resume tailoring (no fabrication)
`data/master_resume.json` is a structured copy of your PDF (`RESUME_PATH`, never modified). The engine only **selects and re-orders** what is in that file: skill categories/items matching the job move first, the most relevant bullets/projects move up, and the summary is assembled from your own summary phrases plus the skills the job asks for **that you actually list**. Skills in the post that are not in your resume (e.g. Golang) are never added. Dates, employers, titles and education are copied verbatim. Output: single-column Helvetica PDF with plain-text headings and `-` bullets (ATS-parsable), saved as `resumes/<job_id>/Abhishek_Chavan_3_5_years_<Company>.pdf`, or `..._Ai_Automation.pdf` when the post names no company (one folder per job, so the attachment name is always clean and jobs never overwrite each other). The PDF embeds the job id so the sender can verify resume ↔ job.
If your real resume changes, edit the JSON; `tests/test_applications.py::test_master_data_matches_original_resume_pdf` checks it against the PDF.

## Tracker columns (`TRACKER_PATH`, sheet `Applications`, one row per Job ID)
Job ID, LinkedIn Post URL, Company Name, Job Title, Job Description, Required Skills, Experience Required, Job Location, Application Instructions, Recruiter Name, Recruiter Email (`Not Available` if none), Customized Resume Path, Email Subject, Email Body or Template, Resume Generation Status, Email Approval Status, Email Sending Status, Email Sent Timestamp, Error Details, Draft Fingerprint.
Statuses: Pending, Resume Generated, Awaiting Approval, Approved, Sending, Sent, Skipped, Failed. You may edit the email body in Excel before approving.
Job ID = LinkedIn activity id if present in the URL, else a hash of the normalized URL (no query/fragment/trailing slash).

## Safeguards
- Recruiter e-mail is taken only from the leads *Email* column or literally from the post text (also `name [at] domain [dot] com`); `noreply@`, image-like strings are ignored; nothing is guessed. No e-mail → `Not Available`, send step skipped.
- Send requires **all** of: approval = Approved, `--send`, valid address, recipient unchanged since draft, resume file present and its embedded job id equal to the row's job id, fingerprint match, no earlier application for that job, and no earlier e-mail to that address (legacy log included; relax with `ALLOW_MULTIPLE_JOBS_PER_RECIPIENT=true`).
- **Duplicate sweep:** before review / approve / send, rows that can never be sent (address already e-mailed, job already sent, same recipient repeated in the batch - first wins) are set to *Skipped*, so they never appear in review, are never approved, and do not use up the per-run cap.
- A row is marked `Sending` before the SMTP call. If the process dies mid-send, the next run flags it `Failed – outcome unknown, check Sent folder` and does **not** resend.
- One failing row never stops the batch (authentication errors abort it). Tracker is saved atomically after every row; a corrupt tracker is reported, never overwritten.
- To regenerate a row's resume/draft, clear its *Resume Generation Status* cell and re-run `apply-prepare`.
- Logs mask recipient addresses (`k***@domain`); passwords/API keys are never logged.

## New environment variables
| Variable | Default | Purpose |
|---|---|---|
| `AUTO_SEND_EMAILS` | `false` | `true` pre-approves drafts at prepare time (`--send` is still required to send) |
| `ALLOW_MULTIPLE_JOBS_PER_RECIPIENT` | `false` | allow a second e-mail to an address already e-mailed |
| `MASTER_RESUME_JSON` | `./data/master_resume.json` | structured master resume |
| `RESUME_OUTPUT_DIR` | `./resumes` | tailored resumes |
| `TRACKER_PATH` | `./data/applications_tracker.xlsx` | tracker workbook |
| `MIN_JD_CHARS` | `40` | shorter posts with <2 recognised skills are skipped |
| `SKIP_EMAIL_DOMAINS` | `gmail.com` | comma-separated domains never e-mailed (e.g. `gmail.com`); also `--skip-domain gmail.com` on any apply-* command; matched rows become *Skipped* |
| `MAX_PER_RUN` | `30` | max e-mails actually sent per `apply-send --send` run (existing setting, now counts only real sends) |
SMTP settings are the existing `SMTP_*` variables (only needed for `--send`).

## Testing without sending
`pytest -q` uses an in-memory fake mailer; no network. For a manual rehearsal run everything except `apply-send --send`, or point `SMTP_HOST` at a local debugging server / your own test mailbox.

## Limitations
- The LinkedIn scraper is not part of this project; this workflow consumes its Excel output. Only the first e-mail in a post is used.
- Skill detection is keyword-based; "Experience Required" is shown so you can skip roles far above your experience (e.g. 7-12 years) during review.
- Tailoring never rewrites bullet wording, so tailored resumes differ in order/emphasis, not phrasing.

### Re-running after new scrapes / sending in batches
- `apply-prepare --excel <new leads>` only adds jobs not already in the tracker.
- Before every review, approve and send, rows that can never be sent (address already e-mailed, job already sent, or same recipient repeated in the batch) are marked **Skipped** automatically, so `apply-review` and `apply-approve --all` only show/approve *new* recipients.
- `MAX_PER_RUN` (default 30, or `--limit`) caps e-mails actually sent per `apply-send --send` run; duplicates do not use it up. Run `apply-send --send` again for the next batch, or raise `MAX_PER_RUN` in `.env`.

## Follow-ups (`apply-followup`)
Recruiters often miss the first e-mail. After some days you can send **one** short follow-up per recruiter, instead of re-sending the application:
```bash
python -m agency_outreach apply-followup --days 20            # DRY RUN: lists who would get a follow-up
python -m agency_outreach apply-followup --days 20 --send     # send (up to MAX_PER_RUN; add --all for no cap)
```
- Only rows with *Email Sending Status = Sent* whose *Email Sent Timestamp* is at least `--days` old (default `FOLLOWUP_AFTER_DAYS`, 14).
- Never to: rows where you typed **Yes** in the tracker's *Replied* column, domains in `SKIP_EMAIL_DOMAINS` (gmail.com by default), anyone already followed up, or the same address twice. Replies are **not** detected automatically - mark them in the *Replied* column first.
- One follow-up per address, ever (stored in the `followups` table). Marked "sending" before the SMTP call, so a crash is never re-sent blindly; permanently rejected addresses are not retried.
- Subject `Following up: <original subject>`, short body, same tailored resume re-attached. Result is recorded in *Follow-up Status* / *Follow-up Sent Timestamp*.
