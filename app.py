import base64
import os
import re
import sqlite3
import uuid
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, flash, g, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CAPTCHA_SIGNALS = [
    "captcha",
    "verify you are human",
    "i'm not a robot",
    "hcaptcha",
    "recaptcha",
    "cloudflare challenge",
]
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "txt"}
DEFAULT_SCAN_PATHS = ["/", "/careers", "/jobs", "/contact", "/about", "/karriere", "/kontakt"]
MAX_LISTINGS = 20

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")
app.config["DATABASE"] = os.environ.get("DATABASE_PATH", str(Path(__file__).parent / "app.db"))
app.config["UPLOAD_FOLDER"] = os.environ.get("UPLOAD_FOLDER", str(Path(__file__).parent / "uploads"))


class GmailDraftService:
    def __init__(self) -> None:
        self.client_id = os.environ.get("GMAIL_CLIENT_ID")
        self.client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
        self.refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN")

    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    def create_draft(
        self,
        to_email: str,
        subject: str,
        body: str,
        attachments: List[Path],
    ) -> Dict[str, str]:
        if not self.enabled():
            return {"mode": "demo", "id": f"demo-{uuid.uuid4().hex[:10]}", "link": ""}

        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except Exception:
            return {"mode": "demo", "id": f"demo-{uuid.uuid4().hex[:10]}", "link": ""}

        credentials = Credentials(
            token=None,
            refresh_token=self.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self.client_id,
            client_secret=self.client_secret,
            scopes=["https://www.googleapis.com/auth/gmail.compose"],
        )

        message = MIMEMultipart()
        message["to"] = to_email
        message["subject"] = subject
        message.attach(MIMEText(body, "plain"))

        for attachment in attachments:
            if not attachment.exists():
                continue
            with attachment.open("rb") as f:
                part = MIMEApplication(f.read(), Name=attachment.name)
            part["Content-Disposition"] = f'attachment; filename="{attachment.name}"'
            message.attach(part)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        service = build("gmail", "v1", credentials=credentials)
        draft = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        draft_id = draft.get("id", "")
        link = f"https://mail.google.com/mail/u/0/#drafts?compose={draft_id}" if draft_id else ""
        return {"mode": "gmail", "id": draft_id, "link": link}


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception: Optional[Exception]) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT NOT NULL,
            keywords TEXT NOT NULL,
            location TEXT,
            resume_path TEXT,
            cover_letter_path TEXT,
            cover_letter_text TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS job_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_id INTEGER NOT NULL,
            job_title TEXT,
            company_name TEXT,
            listing_url TEXT,
            company_website TEXT,
            contact_email TEXT,
            status TEXT NOT NULL,
            note TEXT,
            draft_subject TEXT,
            draft_body TEXT,
            draft_mode TEXT,
            draft_id TEXT,
            draft_link TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (search_id) REFERENCES searches (id)
        );
        """
    )
    db.commit()


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_upload(file_storage) -> Optional[str]:
    if not file_storage or not file_storage.filename:
        return None
    filename = secure_filename(file_storage.filename)
    if not filename or not allowed_file(filename):
        return None
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    unique_name = f"{uuid.uuid4().hex[:8]}_{filename}"
    path = Path(app.config["UPLOAD_FOLDER"]) / unique_name
    file_storage.save(path)
    return str(path)


def fetch_page(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    headers = {"User-Agent": "JobDraftAssistantMVP/1.0"}
    try:
        response = requests.get(url, headers=headers, timeout=12)
    except requests.RequestException:
        return None
    if response.status_code >= 400:
        return None
    return response.text


def detect_captcha(content: str) -> bool:
    lower = content.lower()
    return any(signal in lower for signal in CAPTCHA_SIGNALS)


def extract_emails(content: str) -> List[str]:
    emails = sorted(set(EMAIL_PATTERN.findall(content)))
    return [email for email in emails if not email.lower().endswith(('.png', '.jpg', '.jpeg'))]


def pick_best_email(emails: List[str]) -> Optional[str]:
    if not emails:
        return None
    priorities = ["careers@", "recruiting@", "hr@", "jobs@", "info@", "contact@"]
    lower_map = {email.lower(): email for email in emails}
    for prefix in priorities:
        for email_lower, raw in lower_map.items():
            if email_lower.startswith(prefix):
                return raw
    return emails[0]


def listing_matches(text: str, keywords: List[str], location: str) -> bool:
    lowered = text.lower()
    keyword_match = any(word in lowered for word in keywords)
    location_match = True if not location else location.lower() in lowered
    return keyword_match and location_match


def collect_listing_links(source_url: str, html: str, keywords: List[str], location: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        text = f"{a.get_text(' ', strip=True)} {a['href']}"
        if listing_matches(text, keywords, location):
            absolute_url = urljoin(source_url, a["href"])
            if absolute_url.startswith(("http://", "https://")):
                links.append(absolute_url)
    deduped = []
    seen = set()
    for link in links:
        if link not in seen:
            deduped.append(link)
            seen.add(link)
    return deduped[:MAX_LISTINGS]


def find_company_website(listing_url: str, html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    parsed_listing = urlparse(listing_url)

    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True).lower()
        if any(k in txt for k in ["company", "website", "about employer"]):
            href = urljoin(listing_url, a["href"])
            if urlparse(href).netloc:
                return href

    return f"{parsed_listing.scheme}://{parsed_listing.netloc}"


def extract_title_company(listing_url: str, html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.find("h1") or soup.find("title"))
    title_text = title.get_text(" ", strip=True) if title else "Unknown title"

    company = ""
    og_site = soup.find("meta", attrs={"property": "og:site_name"})
    if og_site and og_site.get("content"):
        company = og_site["content"].strip()
    if not company:
        company = urlparse(listing_url).netloc.replace("www.", "")

    return {"job_title": title_text[:200], "company_name": company[:120]}


def find_contact_email(listing_url: str, listing_html: str, company_website: str) -> Dict[str, Optional[str]]:
    listing_emails = extract_emails(listing_html)
    best = pick_best_email(listing_emails)
    if best:
        return {"email": best, "status": "email_found", "note": "Found email on listing page."}

    for path in DEFAULT_SCAN_PATHS:
        target = urljoin(company_website, path)
        page = fetch_page(target)
        if not page:
            continue
        if detect_captcha(page):
            return {"email": None, "status": "captcha_detected", "note": f"CAPTCHA detected on {target}"}
        emails = extract_emails(page)
        best = pick_best_email(emails)
        if best:
            return {"email": best, "status": "email_found", "note": f"Found email on {target}"}

    if "apply" in listing_html.lower() and "mailto:" not in listing_html.lower():
        return {"email": None, "status": "manual_apply_required", "note": "Listing appears to require platform application."}

    return {"email": None, "status": "no_email_found", "note": "No contact email found on listing/company public pages."}


def build_draft_content(company_name: str, job_title: str, keywords: str, cover_letter_text: str) -> Dict[str, str]:
    subject = f"Application - {job_title} - via website search"
    if cover_letter_text.strip():
        body = cover_letter_text.strip()
    else:
        body = (
            f"Hello {company_name},\n\n"
            f"I found your {job_title} opportunity while searching for '{keywords}'. "
            "Please find my resume and cover letter attached for your review.\n\n"
            "Best regards,\n"
            "Your Name"
        )
    return {"subject": subject[:180], "body": body}


def run_scan(search_row: sqlite3.Row) -> None:
    db = get_db()
    source_html = fetch_page(search_row["source_url"])
    if not source_html:
        db.execute(
            """
            INSERT INTO job_results (search_id, status, note, created_at)
            VALUES (?, 'failed', 'Could not fetch source website.', ?)
            """,
            (search_row["id"], datetime.utcnow().isoformat()),
        )
        db.commit()
        return

    if detect_captcha(source_html):
        db.execute(
            """
            INSERT INTO job_results (search_id, status, note, created_at)
            VALUES (?, 'captcha_detected', 'CAPTCHA detected on source website.', ?)
            """,
            (search_row["id"], datetime.utcnow().isoformat()),
        )
        db.commit()
        return

    keywords = [k.strip().lower() for k in search_row["keywords"].split(",") if k.strip()]
    links = collect_listing_links(search_row["source_url"], source_html, keywords, search_row["location"] or "")

    if not links:
        db.execute(
            """
            INSERT INTO job_results (search_id, status, note, created_at)
            VALUES (?, 'failed', 'No matching listings found on source website.', ?)
            """,
            (search_row["id"], datetime.utcnow().isoformat()),
        )
        db.commit()
        return

    draft_service = GmailDraftService()

    for link in links:
        try:
            listing_html = fetch_page(link)
            if not listing_html:
                db.execute(
                    """
                    INSERT INTO job_results (search_id, listing_url, status, note, created_at)
                    VALUES (?, ?, 'failed', 'Could not fetch listing page.', ?)
                    """,
                    (search_row["id"], link, datetime.utcnow().isoformat()),
                )
                continue

            if detect_captcha(listing_html):
                db.execute(
                    """
                    INSERT INTO job_results (search_id, listing_url, status, note, created_at)
                    VALUES (?, ?, 'captcha_detected', 'CAPTCHA detected on listing page.', ?)
                    """,
                    (search_row["id"], link, datetime.utcnow().isoformat()),
                )
                continue

            details = extract_title_company(link, listing_html)
            company_website = find_company_website(link, listing_html)
            email_info = find_contact_email(link, listing_html, company_website)

            status = "matched"
            draft_subject = None
            draft_body = None
            draft_mode = None
            draft_id = None
            draft_link = None

            if email_info["status"] == "email_found" and email_info["email"]:
                draft_data = build_draft_content(
                    details["company_name"],
                    details["job_title"],
                    search_row["keywords"],
                    search_row["cover_letter_text"] or "",
                )
                attachments = []
                if search_row["resume_path"]:
                    attachments.append(Path(search_row["resume_path"]))
                if search_row["cover_letter_path"]:
                    attachments.append(Path(search_row["cover_letter_path"]))
                draft_result = draft_service.create_draft(
                    email_info["email"],
                    draft_data["subject"],
                    draft_data["body"],
                    attachments,
                )
                draft_subject = draft_data["subject"]
                draft_body = draft_data["body"]
                draft_mode = draft_result["mode"]
                draft_id = draft_result["id"]
                draft_link = draft_result["link"]
                status = "draft_created"
            elif email_info["status"] in {
                "manual_apply_required",
                "no_email_found",
                "captcha_detected",
            }:
                status = email_info["status"]

            db.execute(
                """
                INSERT INTO job_results (
                    search_id, job_title, company_name, listing_url, company_website,
                    contact_email, status, note, draft_subject, draft_body,
                    draft_mode, draft_id, draft_link, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    search_row["id"],
                    details["job_title"],
                    details["company_name"],
                    link,
                    company_website,
                    email_info["email"],
                    status,
                    email_info["note"],
                    draft_subject,
                    draft_body,
                    draft_mode,
                    draft_id,
                    draft_link,
                    datetime.utcnow().isoformat(),
                ),
            )
        except Exception as exc:
            db.execute(
                """
                INSERT INTO job_results (search_id, listing_url, status, note, created_at)
                VALUES (?, ?, 'failed', ?, ?)
                """,
                (search_row["id"], link, f"Unexpected error: {exc}", datetime.utcnow().isoformat()),
            )

    db.commit()


@app.route("/")
def index():
    init_db()
    db = get_db()
    searches = db.execute("SELECT * FROM searches ORDER BY id DESC").fetchall()
    return render_template("index.html", searches=searches)


@app.route("/searches", methods=["POST"])
def create_search():
    init_db()
    source_url = request.form.get("source_url", "").strip()
    keywords = request.form.get("keywords", "").strip()
    location = request.form.get("location", "").strip()
    cover_letter_text = request.form.get("cover_letter_text", "").strip()

    if not source_url or not keywords:
        flash("Source website and keywords are required.")
        return redirect(url_for("index"))

    if not source_url.startswith(("http://", "https://")):
        flash("Source website must start with http:// or https://")
        return redirect(url_for("index"))

    resume_path = save_upload(request.files.get("resume_file"))
    cover_letter_path = save_upload(request.files.get("cover_letter_file"))

    db = get_db()
    cur = db.execute(
        """
        INSERT INTO searches (source_url, keywords, location, resume_path, cover_letter_path, cover_letter_text, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_url,
            keywords,
            location,
            resume_path,
            cover_letter_path,
            cover_letter_text,
            datetime.utcnow().isoformat(),
        ),
    )
    db.commit()

    return redirect(url_for("view_search", search_id=cur.lastrowid))


@app.route("/searches/<int:search_id>")
def view_search(search_id: int):
    init_db()
    db = get_db()
    search = db.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()
    if not search:
        flash("Search not found.")
        return redirect(url_for("index"))
    results = db.execute(
        "SELECT * FROM job_results WHERE search_id = ? ORDER BY id DESC", (search_id,)
    ).fetchall()
    return render_template("search.html", search=search, results=results)


@app.route("/searches/<int:search_id>/scan", methods=["POST"])
def trigger_scan(search_id: int):
    init_db()
    db = get_db()
    search = db.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()
    if not search:
        flash("Search not found.")
        return redirect(url_for("index"))

    db.execute("DELETE FROM job_results WHERE search_id = ?", (search_id,))
    db.commit()

    run_scan(search)
    flash("Scan completed.")
    return redirect(url_for("view_search", search_id=search_id))


if __name__ == "__main__":
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
