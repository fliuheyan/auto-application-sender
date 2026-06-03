# auto-application-sender

Single-user MVP web app for job-website-driven application assistance.

## What it does (MVP)

- Accepts a job source website URL, keyword(s), optional location
- Accepts resume and cover letter uploads
- Scans matching listing links from the source page (conservative parsing)
- Extracts job title, company, listing URL, company website
- Searches listing/company public pages for contact emails
- Creates Gmail drafts (never auto-sends) with resume/cover-letter attachments
- Falls back to **demo draft mode** if Gmail credentials are missing
- Persists searches/results in SQLite

## Statuses

The app records/uses these statuses:

- `job_found` / `matched`
- `email_found`
- `draft_created`
- `no_email_found`
- `manual_apply_required`
- `captcha_detected`
- `failed`

## Quick start

1. Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Run app:

```bash
python app.py
```

3. Open:

- `http://127.0.0.1:5000`

## Gmail draft integration

The app creates Gmail drafts only (no direct send).

### Real Gmail mode (optional)

Set environment variables:

- `GMAIL_CLIENT_ID`
- `GMAIL_CLIENT_SECRET`
- `GMAIL_REFRESH_TOKEN`

And ensure Gmail API scope includes:

- `https://www.googleapis.com/auth/gmail.compose`

When these are present, drafts are created through Gmail API.

### Demo/local mode (default)

If Gmail credentials are absent (or Gmail SDK unavailable), draft creation is recorded in local DB as `draft_mode=demo` with a generated demo draft ID.

## Conservative crawling behavior

- Only follows likely pages (`/careers`, `/jobs`, `/contact`, `/about`, `/karriere`, `/kontakt`)
- Detects CAPTCHA/verification indicators and flags `captcha_detected`
- Does not attempt CAPTCHA bypass

## Test

```bash
python -m unittest discover -v
```
