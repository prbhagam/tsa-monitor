#!/usr/bin/env python3
"""TSA NTC2026 results monitor.

Polls the Tier 1 Winners ("Preliminary Top 24") and Semifinalists pages, detects
newly posted High School events, and emails a digest reporting whether our team
(TEAM_ID) or any tracked students appear in each new event.

Run once per invocation; scheduling (every 10 min) is handled externally by
launchd. See README.md.
"""

import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from html import escape

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Maps an env URL var -> human-readable result type used in subjects/bodies.
PAGES = [
    ("TIER1_URL", "Tier 1 (Preliminary Top 24)"),
    ("SEMIFINALIST_URL", "Semifinalist"),
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

NOT_RELEASED_RE = re.compile(r"have not been released", re.IGNORECASE)
DIVISION_RE = re.compile(r"\(([^)]+)\)\s*$")
TEAM_ENTRY_RE = re.compile(r"^T(\d+)\s+Team\s*#(\d+)", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self):
        load_dotenv(os.path.join(PROJECT_DIR, ".env"))
        self.smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        self.sender = _require("SENDER_EMAIL")
        self.sender_name = os.environ.get("SENDER_NAME", "TSA NTC2026 Monitor").strip()
        self.recipient = _require("RECIPIENT_EMAIL")

        # Email backend: "smtp" (default, used locally) or "brevo" (HTTPS API,
        # required on hosts like Render that block outbound SMTP ports).
        self.email_backend = os.environ.get("EMAIL_BACKEND", "smtp").strip().lower()
        if self.email_backend == "brevo":
            self.brevo_api_key = _require("BREVO_API_KEY")
            self.app_password = os.environ.get("SENDER_APP_PASSWORD", "").strip()
        else:
            self.brevo_api_key = ""
            self.app_password = _require("SENDER_APP_PASSWORD")

        self.urls = {label: os.environ.get(var, "").strip()
                     for var, label in PAGES}

        self.team_id = os.environ.get("TEAM_ID", "").strip()
        self.students = _parse_students(os.environ.get("STUDENTS", ""))
        self.divisions = {d.strip().upper()
                          for d in os.environ.get("DIVISIONS", "HS").split(",")
                          if d.strip()}

        self.state_file = _abspath(os.environ.get("STATE_FILE", "state.json"))
        self.log_file = _abspath(os.environ.get("LOG_FILE", "monitor.log"))


def _require(key):
    val = os.environ.get(key, "").strip()
    if not val:
        sys.exit(f"Missing required config: {key} (check .env)")
    return val


def _abspath(path):
    return path if os.path.isabs(path) else os.path.join(PROJECT_DIR, path)


def _parse_students(raw):
    """Parse 'id:Name,id:Name' into {id: name}."""
    students = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        sid, name = chunk.split(":", 1)
        students[sid.strip()] = name.strip()
    return students


# --------------------------------------------------------------------------- #
# Scraping / parsing
# --------------------------------------------------------------------------- #
def fetch(url):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_page(html):
    """Return a list of event dicts: {name, division, kind, entries}.

    Page layout: <h3>EVENT NAME (HS)</h3> followed by <h4>...</h4> entries until
    the next <h3>. Empty pages contain a 'have not been released' sentinel.
    """
    if NOT_RELEASED_RE.search(html):
        return []

    soup = BeautifulSoup(html, "html.parser")
    events = []
    for h3 in soup.find_all("h3"):
        name = h3.get_text(strip=True)
        m = DIVISION_RE.search(name)
        if not m:
            # Header rows like "Semifinalists" have no (HS)/(MS) suffix.
            continue
        division = m.group(1).strip().upper()

        entries = []
        for sib in h3.find_next_siblings():
            if sib.name == "h3":
                break
            if sib.name == "h4":
                text = sib.get_text(strip=True)
                if text:
                    entries.append(text)
        if not entries:
            continue

        kind = "team" if TEAM_ENTRY_RE.match(entries[0]) else "individual"
        events.append({
            "name": name,
            "division": division,
            "kind": kind,
            "entries": entries,
        })
    return events


def entries_hash(entries):
    joined = "\n".join(sorted(entries))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def find_our_matches(event, team_id, students):
    """Return a dict describing our team/student presence in an event."""
    if event["kind"] == "team":
        teams = []  # list of team-number labels for our team_id
        for entry in event["entries"]:
            m = TEAM_ENTRY_RE.match(entry)
            if m and m.group(1) == team_id:
                teams.append(m.group(2))
        return {"kind": "team", "team_id": team_id, "teams": teams}

    matched = []  # list of (name, id)
    for entry in event["entries"]:
        sid = entry.strip()
        if sid in students:
            matched.append((students[sid], sid))
    return {"kind": "individual", "students": matched}


# --------------------------------------------------------------------------- #
# State diff
# --------------------------------------------------------------------------- #
def diff_state(result_type, events, state, divisions):
    """Find new/updated events for one page; mutate state with current hashes.

    Returns a list of (event, change) tuples where change is 'new' or 'updated'.
    Only events whose division is in `divisions` are considered.
    """
    page_state = state.setdefault(result_type, {})
    changed = []
    for event in events:
        if event["division"] not in divisions:
            continue
        h = entries_hash(event["entries"])
        prev = page_state.get(event["name"])
        if prev is None:
            changed.append((event, "new"))
        elif prev != h:
            changed.append((event, "updated"))
        page_state[event["name"]] = h
    return changed


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def format_event_section(result_type, event, change, cfg):
    matches = find_our_matches(event, cfg.team_id, cfg.students)
    lines = []
    tag = " (UPDATED)" if change == "updated" else ""
    lines.append(f"{'=' * 60}")
    lines.append(f"{event['name']}{tag}")
    lines.append(f"  Result type : {result_type}")
    lines.append(f"  Event type  : {'Team' if event['kind'] == 'team' else 'Individual'}")
    lines.append(f"  Entries     : {len(event['entries'])}")

    if matches["kind"] == "team":
        if matches["teams"]:
            labels = ", ".join(f"Team #{n}" for n in matches["teams"])
            lines.append(f"  >> OUR TEAM {cfg.team_id} IS IN THIS EVENT as {labels}")
        else:
            lines.append(f"  -- Our team {cfg.team_id} is NOT in this event.")
    else:
        if matches["students"]:
            lines.append("  >> OUR STUDENTS IN THIS EVENT:")
            for name, sid in matches["students"]:
                lines.append(f"       - {name} ({sid})")
        else:
            lines.append("  -- None of our students are in this event.")

    return "\n".join(lines)


# --- HTML email styling -----------------------------------------------------
C_NAVY = "#14366e"
C_HIT_BG = "#e8f6ec"
C_HIT_BORDER = "#1e7e34"
C_HIT_TEXT = "#13632a"
C_MISS_BG = "#f1f3f5"
C_MISS_BORDER = "#ced4da"
C_MISS_TEXT = "#6c757d"
C_CARD_BORDER = "#e3e6ea"
C_MUTED = "#6c757d"


def _badge(text, bg, color):
    return (f'<span style="display:inline-block;padding:2px 9px;border-radius:999px;'
            f'font-size:11px;font-weight:600;letter-spacing:.3px;background:{bg};'
            f'color:{color};">{escape(text)}</span>')


def _event_card_html(result_type, event, change, cfg):
    matches = find_our_matches(event, cfg.team_id, cfg.students)
    hit = (matches["kind"] == "team" and matches["teams"]) or \
          (matches["kind"] == "individual" and matches["students"])

    # Badges row.
    badges = [_badge(event["division"], "#e9ecef", "#495057"),
              _badge("Team" if event["kind"] == "team" else "Individual",
                     "#e7eefc", "#2b4f9e")]
    if change == "updated":
        badges.append(_badge("UPDATED", "#fff3cd", "#856404"))
    badges_html = " ".join(badges)

    # Match callout.
    if matches["kind"] == "team":
        if matches["teams"]:
            labels = ", ".join(f"Team&nbsp;#{escape(n)}" for n in matches["teams"])
            callout = f"✅ <strong>Team {escape(cfg.team_id)}</strong> is in this event as {labels}"
        else:
            callout = f"Team {escape(cfg.team_id)} is not in this event."
    else:
        if matches["students"]:
            items = "".join(
                f'<li style="margin:2px 0;">{escape(name)} '
                f'<span style="color:{C_MUTED};">({escape(sid)})</span></li>'
                for name, sid in matches["students"])
            callout = (f"✅ <strong>Our students in this event:</strong>"
                       f'<ul style="margin:6px 0 0;padding-left:20px;">{items}</ul>')
        else:
            callout = "None of our tracked students are in this event."

    co_bg, co_border, co_text = (
        (C_HIT_BG, C_HIT_BORDER, C_HIT_TEXT) if hit
        else (C_MISS_BG, C_MISS_BORDER, C_MISS_TEXT))

    border_left = f"4px solid {C_HIT_BORDER}" if hit else f"4px solid {C_CARD_BORDER}"

    return f"""
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border:1px solid {C_CARD_BORDER};border-left:{border_left};
                    border-radius:8px;margin:0 0 14px;background:#ffffff;">
        <tr><td style="padding:14px 16px;">
          <div style="font-size:16px;font-weight:700;color:#1a1a1a;">{escape(event['name'])}</div>
          <div style="margin:8px 0 12px;">{badges_html}
            <span style="color:{C_MUTED};font-size:12px;margin-left:6px;">
              {len(event['entries'])} placed</span>
          </div>
          <div style="background:{co_bg};border:1px solid {co_border};border-radius:6px;
                      padding:10px 12px;font-size:14px;color:{co_text};">{callout}</div>
        </td></tr>
      </table>"""


def build_email(changes_by_type, cfg):
    """changes_by_type: {result_type: [(event, change), ...]}.

    Returns (subject, text_body, html_body).
    """
    total = sum(len(v) for v in changes_by_type.values())
    div_label = "/".join(sorted(cfg.divisions))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Subject: concise summary.
    if total == 1:
        rt, items = next((rt, v) for rt, v in changes_by_type.items() if v)
        event, _ = items[0]
        subject = f"[TSA NTC2026] New {div_label} result: {event['name']} ({rt})"
    else:
        subject = f"[TSA NTC2026] {total} new {div_label} event results posted"

    # ---- Plain-text body (fallback) ----
    text = [f"New/updated {div_label} event results detected at {ts}.\n"]
    for _var, result_type in PAGES:
        items = changes_by_type.get(result_type)
        if not items:
            continue
        text.append(f"\n########## {result_type} ##########\n")
        for event, change in items:
            text.append(format_event_section(result_type, event, change, cfg))
            text.append("")
    text.append(f"\n{'=' * 60}")
    text.append("Tracked team: " + cfg.team_id)
    text.append("Tracked students: "
                + ", ".join(f"{n} ({i})" for i, n in cfg.students.items()))
    text_body = "\n".join(text)

    # ---- HTML body ----
    sections = []
    for _var, result_type in PAGES:
        items = changes_by_type.get(result_type)
        if not items:
            continue
        cards = "".join(_event_card_html(result_type, e, c, cfg) for e, c in items)
        sections.append(f"""
          <div style="margin:22px 0 6px;">
            <span style="display:inline-block;background:{C_NAVY};color:#fff;
                         font-size:13px;font-weight:700;letter-spacing:.4px;
                         padding:5px 12px;border-radius:6px;">{escape(result_type)}</span>
          </div>
          {cards}""")

    students_footer = ", ".join(f"{escape(n)} ({escape(i)})"
                               for i, n in cfg.students.items())
    headline = ("1 new event" if total == 1 else f"{total} new events")

    html_body = f"""\
<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#eef1f5;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f5;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0"
             style="max-width:640px;width:100%;background:#ffffff;border-radius:12px;
                    overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08);">
        <tr><td style="background:{C_NAVY};padding:22px 24px;">
          <div style="color:#fff;font-size:20px;font-weight:800;">TSA NTC2026 — New Results</div>
          <div style="color:#c7d4ee;font-size:13px;margin-top:4px;">
            {headline} · {escape(div_label)} division · {escape(ts)}</div>
        </td></tr>
        <tr><td style="padding:8px 24px 20px;">
          {''.join(sections)}
        </td></tr>
        <tr><td style="background:#f7f9fc;border-top:1px solid {C_CARD_BORDER};
                       padding:16px 24px;font-size:12px;color:{C_MUTED};line-height:1.6;">
          <strong>Tracked team:</strong> {escape(cfg.team_id)}<br>
          <strong>Tracked students:</strong> {students_footer}
        </td></tr>
      </table>
      <div style="color:#9aa3af;font-size:11px;margin-top:14px;">
        Automated by the South Forsyth TSA results monitor.</div>
    </td></tr>
  </table>
</body></html>"""

    return subject, text_body, html_body


def send_email(cfg, subject, text_body, html_body=None):
    """Dispatch to the configured email backend."""
    if cfg.email_backend == "brevo":
        _send_email_brevo(cfg, subject, text_body, html_body)
    else:
        _send_email_smtp(cfg, subject, text_body, html_body)


def _send_email_smtp(cfg, subject, text_body, html_body=None):
    msg = EmailMessage()
    msg["From"] = cfg.sender
    msg["To"] = cfg.recipient
    msg["Subject"] = subject
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
        server.starttls()
        server.login(cfg.sender, cfg.app_password)
        server.send_message(msg)


def _send_email_brevo(cfg, subject, text_body, html_body=None):
    """Send via Brevo's transactional email HTTP API (port 443) for hosts that
    block outbound SMTP. The sender address must be a verified Brevo sender."""
    payload = {
        "sender": {"email": cfg.sender, "name": cfg.sender_name},
        "to": [{"email": cfg.recipient}],
        "subject": subject,
        "textContent": text_body,
    }
    if html_body:
        payload["htmlContent"] = html_body
    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": cfg.brevo_api_key,
                 "Content-Type": "application/json",
                 "Accept": "application/json"},
        json=payload, timeout=30)
    resp.raise_for_status()


# --------------------------------------------------------------------------- #
# State persistence + logging
# --------------------------------------------------------------------------- #
def load_state(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def log(cfg, message):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {message}"
    print(line)
    try:
        with open(cfg.log_file, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(cfg, dry_run=False):
    existing_state = load_state(cfg.state_file)
    first_run = existing_state is None
    state = existing_state or {}

    changes_by_type = {}
    for var, result_type in PAGES:
        url = cfg.urls.get(result_type)
        if not url:
            log(cfg, f"WARN no URL configured for {result_type}")
            continue
        try:
            html = fetch(url)
        except requests.RequestException as e:
            log(cfg, f"ERROR fetching {result_type}: {e}")
            continue
        events = parse_page(html)
        hs_events = [e for e in events if e["division"] in cfg.divisions]
        log(cfg, f"{result_type}: {len(events)} events parsed "
                 f"({len(hs_events)} in {'/'.join(sorted(cfg.divisions))})")
        changed = diff_state(result_type, events, state, cfg.divisions)
        if changed:
            changes_by_type[result_type] = changed

    total = sum(len(v) for v in changes_by_type.values())

    if first_run and not dry_run:
        # Baseline silently so we don't flood if deployed after results exist.
        save_state(cfg.state_file, state)
        log(cfg, f"First run: baseline saved ({total} events recorded, no email sent).")
        return

    if total == 0:
        log(cfg, "No new events.")
        if not dry_run:
            save_state(cfg.state_file, state)
        return

    subject, text_body, html_body = build_email(changes_by_type, cfg)

    if dry_run:
        log(cfg, f"[DRY RUN] Would send email: {subject}")
        preview_path = _abspath("email_preview.html")
        with open(preview_path, "w") as f:
            f.write(html_body)
        print("\n----- EMAIL PREVIEW -----")
        print("Subject:", subject)
        print(text_body)
        print(f"\n[HTML preview written to {preview_path}]")
        print("----- END PREVIEW -----")
        return

    try:
        send_email(cfg, subject, text_body, html_body)
        log(cfg, f"Email sent: {subject}")
    except Exception as e:  # noqa: BLE001 - report any SMTP failure, keep state unsaved
        log(cfg, f"ERROR sending email: {e}")
        # Do not save state, so the next run retries these events.
        return

    save_state(cfg.state_file, state)


def main():
    parser = argparse.ArgumentParser(description="TSA NTC2026 results monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and print; do not email or write state.")
    parser.add_argument("--test-email", action="store_true",
                        help="Send a test email to verify SMTP, then exit.")
    parser.add_argument("--reset", action="store_true",
                        help="Delete the state file, then exit.")
    args = parser.parse_args()

    cfg = Config()

    if args.reset:
        if os.path.exists(cfg.state_file):
            os.remove(cfg.state_file)
            log(cfg, "State file deleted.")
        else:
            log(cfg, "No state file to delete.")
        return

    if args.test_email:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            send_email(cfg, "[TSA NTC2026] Test email",
                       f"This is a test from the TSA results monitor at {ts}.\n"
                       f"If you received this, SMTP is configured correctly.")
            log(cfg, "Test email sent.")
        except Exception as e:  # noqa: BLE001
            log(cfg, f"ERROR sending test email: {e}")
            sys.exit(1)
        return

    run(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
