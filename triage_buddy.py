"""
Triage Buddy - a self-hosted GitHub issue triage bot powered by Gemma 4 (via Ollama).

It watches one repo, and for every NEW issue it:
  * asks Gemma 4 (running locally) to classify it, spot missing info and duplicates,
    and read any screenshots,
  * posts ONE friendly comment,
  * adds labels.

It never closes, locks or deletes anything. A human maintainer always makes the final call.
Comment "/triage" on an issue (as a maintainer) to run it again.

Run:  python triage_buddy.py
"""

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

GITHUB_API = "https://api.github.com"
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = os.getenv("MODEL", "gemma4:e4b")
POLL_SECONDS = 15
MAX_IMAGES = 2
MAX_IMAGE_BYTES = 8 * 1024 * 1024

ALLOWED_LABELS = {
    # name: (colour, description)
    "bug": ("d73a4a", "Something isn't working"),
    "feature": ("a2eeef", "New feature or request"),
    "docs": ("0075ca", "Improvements or additions to documentation"),
    "question": ("d876e3", "Further information is requested"),
    "needs-info": ("fbca04", "Waiting for more details from the author"),
    "possible-duplicate": ("cfd3d7", "Might duplicate another issue"),
    "needs-human-review": ("b60205", "A maintainer should take a closer look"),
}
ISSUE_TYPES = ["bug", "feature", "docs", "question"]
MAINTAINER_ROLES = {"OWNER", "MEMBER", "COLLABORATOR"}

FOOTER = "🤖 Triage Buddy · Gemma 4 running locally · a human maintainer makes the final call"

DEFAULT_CONTRIBUTING = """\
Default issue checklist (this repo has no CONTRIBUTING.md):
- Bug reports: steps to reproduce, expected vs actual behaviour, version numbers,
  operating system, and the full error message or stack trace.
- Feature requests: the problem it solves and a short example of how it would be used.
- Docs issues: a link to the page and what is wrong or missing.
- Questions: what you already tried.
"""

# ---------------------------------------------------------------------------
# Console logging (colourful, no secrets ever)
# ---------------------------------------------------------------------------

os.system("")  # turns on ANSI colours in the Windows console
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

COLOURS = {"info": "\033[90m", "work": "\033[96m", "ok": "\033[92m",
           "warn": "\033[93m", "error": "\033[91m"}
RESET = "\033[0m"


def log(message, kind="info"):
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"{COLOURS.get(kind, '')}{stamp}  {message}{RESET}", flush=True)


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

github = requests.Session()
github.headers.update({
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "triage-buddy",
})


def setup_github(token):
    """Attach the token to the GitHub session. The token is never printed."""
    if token:
        github.headers["Authorization"] = f"Bearer {token}"


def gh(method, path, **kwargs):
    url = path if path.startswith("http") else f"{GITHUB_API}{path}"
    resp = github.request(method, url, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp


def gh_paginated(path, params=None, max_pages=5):
    """GET a list endpoint, following 'next' links up to max_pages."""
    items, url = [], path
    for _ in range(max_pages):
        resp = gh("GET", url, params=params)
        items.extend(resp.json())
        url = resp.links.get("next", {}).get("url")
        params = None  # the next link already contains the query string
        if not url:
            break
    return items


def list_open_issues(repo):
    issues = gh_paginated(f"/repos/{repo}/issues",
                          params={"state": "open", "per_page": 100})
    return [i for i in issues if "pull_request" not in i]


def fetch_contributing(repo):
    """Return CONTRIBUTING.md text from the repo, or the default checklist."""
    for path in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md"):
        try:
            resp = github.get(f"{GITHUB_API}/repos/{repo}/contents/{path}",
                              headers={"Accept": "application/vnd.github.raw"},
                              timeout=30)
            if resp.status_code == 200 and resp.text.strip():
                return resp.text[:4000]
        except requests.RequestException:
            pass
    return DEFAULT_CONTRIBUTING


def ensure_labels(repo):
    """Create our labels in the repo if they don't exist yet (never edits or deletes)."""
    for name, (colour, description) in ALLOWED_LABELS.items():
        resp = github.post(f"{GITHUB_API}/repos/{repo}/labels", timeout=30,
                           json={"name": name, "color": colour, "description": description})
        if resp.status_code not in (201, 422):  # 422 = label already exists
            log(f"Could not create label '{name}' (HTTP {resp.status_code})", "warn")


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------

URL_PATTERN = re.compile(r"https?://[^\s)\"'<>\]]+")


def extract_image_urls(body):
    urls = []
    for url in URL_PATTERN.findall(body or ""):
        path = urlparse(url).path.lower()
        is_attachment = "github.com/user-attachments/assets/" in url
        if (is_attachment or path.endswith((".png", ".jpg", ".jpeg"))) and url not in urls:
            urls.append(url)
    return urls


def download_images(urls):
    """Download up to MAX_IMAGES PNG/JPEG images and return them base64-encoded."""
    images = []
    for url in urls:
        if len(images) >= MAX_IMAGES:
            break
        try:
            host = urlparse(url).hostname or ""
            # Only send the token to github.com itself; requests drops it on redirects to other hosts.
            headers = {} if host == "github.com" else {"Authorization": None}
            resp = github.get(url, headers=headers, timeout=30)
            data = resp.content
            if resp.status_code != 200 or len(data) > MAX_IMAGE_BYTES:
                continue
            if data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8"):
                images.append(base64.b64encode(data).decode("ascii"))
        except requests.RequestException:
            continue
    return images


# ---------------------------------------------------------------------------
# Gemma 4 via Ollama
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are Triage Buddy, a friendly assistant that helps open-source maintainers triage GitHub issues.
You never make final decisions; a human maintainer does.

Reply ONLY with a JSON object with exactly these keys:
{
  "type": "bug" | "feature" | "docs" | "question",
  "missing_info": [list of short strings: information the author should add, based on the contributing guide; [] if nothing is missing],
  "duplicate_of": [list of issue numbers from the provided list that describe the SAME problem; [] if none],
  "low_effort": true | false   (true if the issue is spam, empty, or too vague to act on),
  "screenshot_findings": "error text, versions, stack traces or other facts read from the attached screenshots; empty string if no screenshots",
  "comment": "a short, warm markdown reply to the author: thank them, summarise what you understood, and kindly ask for anything missing"
}

Rules:
- Only list a duplicate if it is clearly the same problem, not just the same topic.
- Do not repeat the missing_info checklist or the screenshot findings inside "comment"; they are shown separately.
- Never promise a fix or a timeline.
"""


def build_user_message(issue, contributing, other_issues, image_count):
    others = "\n".join(f"#{n}: {t}" for n, t in other_issues) or "(no other issues)"
    screenshots = (f"{image_count} screenshot(s) from the issue are attached. Read them carefully."
                   if image_count else "No screenshots are attached.")
    return f"""\
CONTRIBUTING GUIDE:
{contributing}

OTHER ISSUES IN THIS REPO (number: title):
{others}

NEW ISSUE #{issue['number']}
Title: {issue.get('title', '')}
Body:
{(issue.get('body') or '(empty)')[:6000]}

{screenshots}
Respond with the JSON object only."""


def parse_gemma_json(text):
    text = text.strip()
    if text.startswith("```"):  # strip accidental ```json fences
        text = text.strip("`").removeprefix("json").strip()
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("comment"), str):
        raise ValueError("JSON is missing the 'comment' field")
    return data


def normalize_result(data, valid_numbers, own_number):
    """Clean up Gemma's answer so the rest of the code can trust its shape."""
    def as_list(value):
        return value if isinstance(value, list) else ([value] if value else [])

    duplicates = []
    for value in as_list(data.get("duplicate_of")):
        try:
            number = int(str(value).lstrip("#"))
        except ValueError:
            continue
        if number != own_number and number in valid_numbers and number not in duplicates:
            duplicates.append(number)

    issue_type = str(data.get("type", "")).lower().strip()
    return {
        "type": issue_type if issue_type in ISSUE_TYPES else None,
        "missing_info": [str(x).strip() for x in as_list(data.get("missing_info")) if str(x).strip()],
        "duplicate_of": duplicates,
        "low_effort": data.get("low_effort") is True or str(data.get("low_effort")).lower() == "true",
        "screenshot_findings": str(data.get("screenshot_findings") or "").strip(),
        "comment": data["comment"].strip(),
    }


def ask_gemma(issue, contributing, other_issues, images):
    """Ask Gemma 4 to triage one issue. Retries once on bad JSON. Returns a dict or None."""
    payload = {
        "model": MODEL,
        "stream": False,
        "format": "json",
        "think": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": build_user_message(issue, contributing, other_issues, len(images)),
             "images": images},
        ],
    }
    valid_numbers = {n for n, _ in other_issues}
    tag = f"[#{issue['number']}]"

    for attempt in (1, 2):
        try:
            resp = requests.post(OLLAMA_URL, json=payload, timeout=600)
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
            return normalize_result(parse_gemma_json(content), valid_numbers, issue["number"])
        except requests.ConnectionError:
            log(f"{tag} cannot reach Ollama at localhost:11434 - is it running?", "error")
            return None
        except requests.RequestException as e:
            log(f"{tag} Ollama error on attempt {attempt}: {e}", "warn")
        except (ValueError, KeyError) as e:  # json.JSONDecodeError is a ValueError
            log(f"{tag} Gemma returned bad JSON on attempt {attempt}: {e}", "warn")
    return None


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------

def build_comment(result):
    parts = [result["comment"]]
    if result["screenshot_findings"]:
        parts.append(f"**🔍 From your screenshot:**\n{result['screenshot_findings']}")
    if result["duplicate_of"]:
        links = ", ".join(f"#{n}" for n in result["duplicate_of"])
        parts.append(f"🔁 Possibly related: {links}")
    if result["missing_info"]:
        checklist = "\n".join(f"- [ ] {item}" for item in result["missing_info"])
        parts.append(f"**To help a maintainer pick this up, could you add:**\n{checklist}")
    parts.append(f"---\n<sub>{FOOTER}</sub>")
    return "\n\n".join(parts)


def pick_labels(result):
    labels = [result["type"]] if result["type"] else ["needs-human-review"]
    if result["missing_info"]:
        labels.append("needs-info")
    if result["duplicate_of"]:
        labels.append("possible-duplicate")
    if result["low_effort"] and "needs-human-review" not in labels:
        labels.append("needs-human-review")
    return [l for l in labels if l in ALLOWED_LABELS]


def triage(repo, issue, open_issues):
    number = issue["number"]
    tag = f"[#{number}]"
    log(f"{tag} triaging... \"{issue.get('title', '')[:60]}\"", "work")
    started = time.time()

    contributing = fetch_contributing(repo)
    other_issues = [(i["number"], i["title"]) for i in open_issues if i["number"] != number]
    image_urls = extract_image_urls(issue.get("body"))
    images = download_images(image_urls)
    if image_urls:
        log(f"{tag} found {len(image_urls)} image link(s), sending {len(images)} to Gemma")

    result = ask_gemma(issue, contributing, other_issues, images)
    if result is None:
        log(f"{tag} skipped: no usable answer from Gemma (comment /triage to retry)", "error")
        return

    labels = pick_labels(result)
    # The only write actions: one comment + labels. Never close, lock or delete.
    gh("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": build_comment(result)})
    gh("POST", f"/repos/{repo}/issues/{number}/labels", json={"labels": labels})
    log(f"{tag} done: labels={labels}, {time.time() - started:.1f}s", "ok")


def safe_triage(repo, issue, open_issues):
    try:
        triage(repo, issue, open_issues)
    except requests.HTTPError as e:
        log(f"[#{issue['number']}] GitHub error: HTTP {e.response.status_code}", "error")
    except Exception as e:
        log(f"[#{issue['number']}] unexpected error: {e}", "error")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    repo = os.getenv("REPO", "").strip()
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not repo or not token or token == "paste_your_token_here":
        log("Please set REPO and GITHUB_TOKEN in your .env file (see .env.example).", "error")
        sys.exit(1)
    setup_github(token)

    log(f"Triage Buddy starting for {repo} with model {MODEL}", "work")
    try:
        open_issues = list_open_issues(repo)
    except requests.HTTPError as e:
        code = e.response.status_code
        hint = {401: "token is invalid", 403: "token lacks permission", 404: "repo not found or token can't see it"}
        log(f"Cannot read {repo}: HTTP {code} ({hint.get(code, 'GitHub error')})", "error")
        sys.exit(1)
    except requests.RequestException as e:
        log(f"Cannot reach GitHub: {e}", "error")
        sys.exit(1)

    ensure_labels(repo)
    seen = {i["number"] for i in open_issues}
    handled_comments = set()
    started_at = comments_since = datetime.now(timezone.utc)
    log(f"Marked {len(seen)} existing issue(s) as seen. Watching for new ones every {POLL_SECONDS}s "
        f"(Ctrl+C to stop).", "ok")

    while True:
        try:
            time.sleep(POLL_SECONDS)
            poll_started = datetime.now(timezone.utc)
            open_issues = list_open_issues(repo)

            # 1) New issues
            for issue in sorted(open_issues, key=lambda i: i["number"]):
                if issue["number"] not in seen:
                    seen.add(issue["number"])
                    safe_triage(repo, issue, open_issues)

            # 2) "/triage" commands from maintainers
            comments = gh("GET", f"/repos/{repo}/issues/comments",
                          params={"since": iso(comments_since - timedelta(seconds=60)),
                                  "sort": "created", "direction": "asc", "per_page": 100}).json()
            for c in comments:
                if c["id"] in handled_comments:
                    continue
                handled_comments.add(c["id"])
                created = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
                if created < started_at:  # ignore commands from before the bot started
                    continue
                if not (c.get("body") or "").strip().lower().startswith("/triage"):
                    continue
                number = int(c["issue_url"].rsplit("/", 1)[1])
                if c.get("author_association") not in MAINTAINER_ROLES:
                    log(f"[#{number}] ignoring /triage from non-maintainer @{c['user']['login']}")
                    continue
                issue = gh("GET", f"/repos/{repo}/issues/{number}").json()
                if "pull_request" in issue:
                    continue
                log(f"[#{number}] /triage requested by @{c['user']['login']}", "work")
                safe_triage(repo, issue, open_issues)

            comments_since = poll_started
        except KeyboardInterrupt:
            log("Stopped. Bye!", "work")
            break
        except requests.RequestException as e:
            log(f"Polling failed, will retry: {e}", "warn")
        except Exception as e:
            log(f"Unexpected error, will retry: {e}", "error")


if __name__ == "__main__":
    main()
