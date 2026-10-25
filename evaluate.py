"""
evaluate.py - measure how well Triage Buddy spots duplicate issues. READ-ONLY.

It takes a public repo, finds its most recent closed issues labelled "duplicate"
(or similar), works out which issue each one duplicates from the maintainers' comments,
then asks Gemma 4 the same duplicate question Triage Buddy asks. Nothing is posted.

For each duplicate, Gemma sees the real original mixed in with ~30 other recent issues
from the repo, and we check whether it picks out the original.

Run:  python evaluate.py facebook/react
      python evaluate.py facebook/react --limit 10
"""

import argparse
import os
import random
import re
import sys
import time

import requests

from triage_buddy import (MODEL, ask_gemma, download_images, extract_image_urls,
                          fetch_contributing, gh, gh_paginated, log, setup_github)

DISTRACTORS = 30


def find_duplicate_labels(repo):
    labels = gh_paginated(f"/repos/{repo}/labels", params={"per_page": 100})
    return [l["name"] for l in labels if "duplicate" in l["name"].lower()]


def closed_duplicates(repo, labels, limit):
    found = {}
    for label in labels:
        query = f'repo:{repo} is:issue is:closed label:"{label}"'
        items = gh("GET", "/search/issues",
                   params={"q": query, "sort": "created", "order": "desc", "per_page": limit}).json()["items"]
        for item in items:
            found[item["number"]] = item
    newest = sorted(found.values(), key=lambda i: i.get("closed_at") or "", reverse=True)
    return newest[:limit]


def find_original(repo, issue):
    """Read the issue's comments to find the number it was marked a duplicate of."""
    own = issue["number"]
    ref = rf"(?:#|https://github\.com/{re.escape(repo)}/issues/)(\d+)"
    comments = gh("GET", issue["comments_url"], params={"per_page": 100}).json()
    texts = [c.get("body") or "" for c in comments]

    strict = re.compile(rf"(?:duplicate\s+of|dupe\s+of|duplicates)\s+{ref}", re.IGNORECASE)
    for text in texts:
        for match in strict.findall(text):
            if int(match) != own:
                return int(match)
    for text in texts:  # looser: a comment mentioning "duplicate" plus an issue reference
        if "duplicat" in text.lower():
            for match in re.findall(ref, text):
                if int(match) != own:
                    return int(match)
    return None


def main():
    parser = argparse.ArgumentParser(description="Read-only duplicate detection benchmark.")
    parser.add_argument("repo", help='public repo, e.g. "facebook/react"')
    parser.add_argument("--limit", type=int, default=20, help="how many duplicate issues to fetch")
    args = parser.parse_args()
    repo = args.repo.strip()

    # A token is optional here (public data only) but raises GitHub's rate limit a lot.
    from dotenv import load_dotenv
    load_dotenv()
    token = os.getenv("GITHUB_TOKEN", "").strip()
    setup_github(token if token != "paste_your_token_here" else "")

    log(f"Evaluating duplicate detection on {repo} with {MODEL} (read-only)", "work")
    try:
        labels = find_duplicate_labels(repo)
        if not labels:
            log(f"No label containing 'duplicate' found in {repo}.", "error")
            sys.exit(1)
        log(f"Using label(s): {labels}")
        dupes = closed_duplicates(repo, labels, args.limit)

        pool = [i for i in gh("GET", f"/repos/{repo}/issues",
                              params={"state": "all", "per_page": 100}).json()
                if "pull_request" not in i]
        contributing = fetch_contributing(repo)
    except requests.HTTPError as e:
        log(f"GitHub error: HTTP {e.response.status_code} (rate limited? add GITHUB_TOKEN to .env)", "error")
        sys.exit(1)

    detected, tested, timings = 0, 0, []
    for dupe in dupes:
        tag = f"[#{dupe['number']}]"
        try:
            original_number = find_original(repo, dupe)
            if original_number is None:
                log(f"{tag} skipped: couldn't tell which issue it duplicates")
                continue
            original = gh("GET", f"/repos/{repo}/issues/{original_number}").json()
            if "pull_request" in original:
                log(f"{tag} skipped: points at a pull request")
                continue
        except requests.HTTPError as e:
            log(f"{tag} skipped: GitHub error HTTP {e.response.status_code}", "warn")
            continue

        others = [i for i in pool if i["number"] not in (dupe["number"], original_number)]
        candidates = [(i["number"], i["title"]) for i in others[:DISTRACTORS]]
        candidates.append((original["number"], original["title"]))
        random.Random(dupe["number"]).shuffle(candidates)

        log(f"{tag} checking (true original: #{original_number})...", "work")
        started = time.time()
        images = download_images(extract_image_urls(dupe.get("body")))
        result = ask_gemma(dupe, contributing, candidates, images)
        elapsed = time.time() - started
        if result is None:
            log(f"{tag} skipped: no usable answer from Gemma", "error")
            continue

        tested += 1
        timings.append(elapsed)
        if original_number in result["duplicate_of"]:
            detected += 1
            log(f"{tag} HIT  -> flagged {result['duplicate_of']} ({elapsed:.1f}s)", "ok")
        else:
            log(f"{tag} MISS -> flagged {result['duplicate_of'] or 'nothing'} ({elapsed:.1f}s)", "warn")

    if not tested:
        log("No issues could be evaluated.", "error")
        sys.exit(1)
    avg = sum(timings) / len(timings)
    log(f"Detected {detected}/{tested} duplicates, avg {avg:.1f}s per issue "
        f"({repo}, {MODEL}, original hidden among {DISTRACTORS} other issues)", "ok")


if __name__ == "__main__":
    main()
