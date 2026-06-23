#!/usr/bin/env python3
"""HTTP wrapper around tsa_monitor for Render + cron-job.org hosting.

cron-job.org pings GET/POST /run every ~10 minutes with a shared secret token;
each ping performs one monitor check. The scheduling lives in cron-job.org, the
compute lives in Render, and tsa_monitor.py itself is unchanged.

State persistence: Render's filesystem is ephemeral, so state.json is read from
and written back to the GitHub repo on a dedicated branch (STATE_BRANCH) via the
Contents API. We use a side branch (not the deploy branch) so state commits don't
trigger Render redeploys. tsa_monitor reads/writes its state file on local disk;
this wrapper syncs that file to GitHub around each run.
"""

import base64
import hmac
import os
import socket
import threading
from datetime import datetime

import requests
from flask import Flask, request

import tsa_monitor

# Render containers can resolve hosts (e.g. smtp.gmail.com) to an IPv6 (AAAA)
# address but have no IPv6 route, so smtplib fails with
# "[Errno 101] Network is unreachable". Force IPv4-only resolution for all
# outbound connections; IPv4 egress works fine (the GitHub API calls use it).
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(*args, **kwargs):
    results = _orig_getaddrinfo(*args, **kwargs)
    ipv4 = [r for r in results if r[0] == socket.AF_INET]
    return ipv4 or results


socket.getaddrinfo = _ipv4_only_getaddrinfo

app = Flask(__name__)

RUN_TOKEN = os.environ.get("RUN_TOKEN", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")          # "owner/repo"
STATE_BRANCH = os.environ.get("STATE_BRANCH", "monitor-state")
STATE_PATH = os.environ.get("STATE_GITHUB_PATH", "state.json")
LOCAL_STATE = os.path.join(tsa_monitor.PROJECT_DIR, "state.json")

# gunicorn runs a single worker (see startCommand), so this lock serializes
# overlapping pings within the process and prevents racing on GitHub state.
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _authorized(req):
    """Fail closed: require a matching token via header or ?token= query."""
    if not RUN_TOKEN:
        return False
    supplied = req.headers.get("X-Run-Token") or req.args.get("token", "")
    return hmac.compare_digest(supplied, RUN_TOKEN)


# --------------------------------------------------------------------------- #
# GitHub-backed state (Contents API)
# --------------------------------------------------------------------------- #
def _gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_url():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_PATH}"


def gh_pull_state():
    """Return (content, sha). content/sha are None if no state file exists yet."""
    resp = requests.get(_gh_url(), headers=_gh_headers(),
                        params={"ref": STATE_BRANCH}, timeout=20)
    if resp.status_code == 404:
        return None, None
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def gh_push_state(content, sha):
    """Create or update the state file on STATE_BRANCH."""
    payload = {
        "message": "Update monitor state",
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": STATE_BRANCH,
    }
    if sha:  # omit on first creation
        payload["sha"] = sha
    resp = requests.put(_gh_url(), headers=_gh_headers(), json=payload, timeout=20)
    resp.raise_for_status()


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _text(body, code):
    """Tiny plain-text response. Bodies are bounded so cron-job.org never aborts
    on response size, and errors stay greppable in its execution history."""
    return (body[:200] + "\n"), code, {"Content-Type": "text/plain; charset=utf-8"}


def _log_http_error(label, e):
    """Print a GitHub API failure to stdout (-> Render logs), including the
    response body, which carries GitHub's specific reason (e.g. permissions)."""
    resp = getattr(e, "response", None)
    detail = f" [{resp.status_code}] {resp.text[:300]}" if resp is not None else ""
    print(f"[{label}] FAILED: {e}{detail}", flush=True)


@app.route("/")
def health():
    return _text("ok", 200)


@app.route("/run", methods=["GET", "POST"])
def run_endpoint():
    if not _authorized(request):
        return _text("unauthorized", 401)

    with _lock:
        # 1. Pull current state from GitHub onto local disk for tsa_monitor.
        try:
            content, sha = gh_pull_state()
        except requests.RequestException as e:
            _log_http_error("state pull", e)
            return _text(f"state pull failed: {e}", 502)

        if content is None:
            # No saved state yet -> let tsa_monitor do a silent first-run baseline.
            if os.path.exists(LOCAL_STATE):
                os.remove(LOCAL_STATE)
        else:
            with open(LOCAL_STATE, "w") as f:
                f.write(content)

        # 2. Run one monitor check. Catch BaseException so a SystemExit from
        #    missing-config validation becomes a 500 rather than killing the worker.
        try:
            cfg = tsa_monitor.Config()
            tsa_monitor.run(cfg)
        except BaseException as e:  # noqa: BLE001
            return _text(f"monitor failed: {e}", 500)

        # 3. Push state back to GitHub if it changed.
        new_content = None
        if os.path.exists(LOCAL_STATE):
            with open(LOCAL_STATE) as f:
                new_content = f.read()

        changed = new_content is not None and new_content != content
        if changed:
            try:
                gh_push_state(new_content, sha)
            except requests.RequestException as e:
                _log_http_error("state push", e)
                return _text(f"state push failed: {e}", 502)

        return _text("ok changed" if changed else "ok", 200)


@app.route("/send-test-email", methods=["GET", "POST"])
def send_test_email():
    """Send a one-off test email using the live Render SMTP config. Verifies the
    Render -> Gmail mail path without scraping, faking results, or touching state.
    """
    if not _authorized(request):
        return _text("unauthorized", 401)
    try:
        cfg = tsa_monitor.Config()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tsa_monitor.send_email(
            cfg,
            "[TSA NTC2026] Test email (Render)",
            f"Test from the Render-hosted monitor at {ts}.\n"
            f"If you received this, SMTP from Render works.")
    except BaseException as e:  # noqa: BLE001
        print(f"[test email] FAILED: {e}", flush=True)
        return _text(f"test email failed: {e}", 500)
    return _text("ok sent", 200)


if __name__ == "__main__":
    # Local dev only; Render uses gunicorn (see render.yaml startCommand).
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
