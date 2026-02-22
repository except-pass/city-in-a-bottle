#!/usr/bin/env python3
"""
Auto-merge engine for City in a Bottle.

Scans approved repos for PRs that meet the democratic approval threshold
(2 approvals, no outstanding rejections) and merges them automatically.
Posts results to Zulip #system channel.

Protected paths (src/mcp_servers/, infra/, .claude/governance/, src/runner/)
additionally require an approval from the operator user.

Usage:
    python scripts/merge_bot.py
    python scripts/merge_bot.py --dry-run
    python scripts/merge_bot.py --zulip-email bot@example.com --zulip-api-key KEY

Environment:
    FORGEJO_TOKEN - Operator Forgejo API token
    FORGEJO_URL   - Forgejo base URL (default: http://localhost:3000)
    FORGEJO_ORG   - Forgejo organization (default: workspace)
    ZULIP_BOT_EMAIL - Zulip bot email for notifications
    ZULIP_API_KEY   - Zulip bot API key
"""

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FORGEJO_URL = os.environ.get("FORGEJO_URL", "http://localhost:3000")
FORGEJO_ORG = os.environ.get("FORGEJO_ORG", "workspace")
APPROVED_REPOS = ["agent-contributions", "agent-economy"]
REQUIRED_APPROVALS = 2
PROTECTED_PATHS = ["src/mcp_servers/", "infra/", ".claude/governance/", "src/runner/"]
OPERATOR_USER = "operator"

ZULIP_URL = os.environ.get("ZULIP_URL", "https://localhost:8443")

REPO_DIR = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Forgejo helpers
# ---------------------------------------------------------------------------

def get_forgejo_token() -> str | None:
    """Get operator Forgejo token from env or .claude/settings.local.json."""
    token = os.environ.get("FORGEJO_TOKEN")
    if token:
        return token

    settings_path = REPO_DIR / ".claude" / "settings.local.json"
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
            token = (
                settings.get("mcpServers", {})
                .get("forgejo", {})
                .get("env", {})
                .get("FORGEJO_TOKEN")
            )
            if token:
                return token
        except (json.JSONDecodeError, KeyError):
            pass

    return None


def forgejo_get(client: httpx.Client, path: str) -> tuple[int, dict | list]:
    """GET request to Forgejo API. Returns (status_code, json)."""
    resp = client.get(f"{FORGEJO_URL}/api/v1{path}")
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"text": resp.text}


def forgejo_post(client: httpx.Client, path: str, data: dict) -> tuple[int, dict]:
    """POST request to Forgejo API. Returns (status_code, json)."""
    resp = client.post(f"{FORGEJO_URL}/api/v1{path}", json=data)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"text": resp.text}


def forgejo_patch(client: httpx.Client, path: str, data: dict) -> tuple[int, dict]:
    """PATCH request to Forgejo API. Returns (status_code, json)."""
    resp = client.patch(f"{FORGEJO_URL}/api/v1{path}", json=data)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"text": resp.text}


# ---------------------------------------------------------------------------
# Zulip helpers
# ---------------------------------------------------------------------------

def post_to_zulip(
    zulip_email: str | None,
    zulip_api_key: str | None,
    message: str,
) -> bool:
    """Post a message to the Zulip #system channel.

    Returns True on success, False on failure. Silently returns False
    if credentials are not available.
    """
    if not zulip_email or not zulip_api_key:
        return False

    try:
        with httpx.Client(verify=False, timeout=15) as client:
            resp = client.post(
                f"{ZULIP_URL}/api/v1/messages",
                auth=(zulip_email, zulip_api_key),
                data={
                    "type": "stream",
                    "to": "system",
                    "topic": "merge-bot",
                    "content": message,
                },
            )
            if resp.status_code == 200 and resp.json().get("result") == "success":
                return True
            print(f"  Zulip post warning: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"  Zulip post error: {e}")

    return False


# ---------------------------------------------------------------------------
# Review logic
# ---------------------------------------------------------------------------

def get_review_verdict(reviews: list[dict]) -> tuple[int, bool, set[str]]:
    """Analyse reviews for a PR.

    Returns:
        (approval_count, has_outstanding_rejection, approver_usernames)

    For each reviewer, only the *latest* review counts.  If the latest
    review is APPROVED it counts as an approval; if REQUEST_CHANGES it
    counts as an outstanding rejection.
    """
    # Deduplicate: keep only the latest review per reviewer
    latest_by_user: dict[str, dict] = {}
    for review in reviews:
        user = review.get("user", {}).get("login", "")
        if not user:
            continue
        # Reviews are returned in chronological order; later ones overwrite.
        latest_by_user[user] = review

    approvals = 0
    has_rejection = False
    approver_users: set[str] = set()

    for user, review in latest_by_user.items():
        state = review.get("state", "").upper()
        if state == "APPROVED":
            approvals += 1
            approver_users.add(user)
        elif state == "REQUEST_CHANGES":
            has_rejection = True

    return approvals, has_rejection, approver_users


def touches_protected_path(changed_files: list[dict]) -> bool:
    """Check if any changed file falls under a protected path."""
    for f in changed_files:
        filename = f.get("filename", "")
        for prefix in PROTECTED_PATHS:
            if filename.startswith(prefix):
                return True
    return False


# ---------------------------------------------------------------------------
# Main merge logic
# ---------------------------------------------------------------------------

def process_repo(
    client: httpx.Client,
    repo: str,
    dry_run: bool,
    zulip_email: str | None,
    zulip_api_key: str | None,
) -> list[dict]:
    """Process all open PRs for a single repo.

    Returns a list of result dicts for reporting.
    """
    owner = FORGEJO_ORG
    results: list[dict] = []

    # Fetch open PRs targeting main
    status, prs = forgejo_get(client, f"/repos/{owner}/{repo}/pulls?state=open&limit=50")
    if status != 200:
        print(f"  Could not list PRs for {owner}/{repo}: {status}")
        return results

    if not isinstance(prs, list):
        print(f"  Unexpected response listing PRs for {owner}/{repo}: {prs}")
        return results

    for pr in prs:
        number = pr.get("number")
        title = pr.get("title", "")
        author = pr.get("user", {}).get("login", "unknown")
        base = pr.get("base", {}).get("ref", "")

        if base != "main":
            continue

        print(f"\n  PR #{number}: '{title}' by {author}")

        # Get reviews
        rev_status, reviews = forgejo_get(
            client, f"/repos/{owner}/{repo}/pulls/{number}/reviews"
        )
        if rev_status != 200:
            print(f"    Could not fetch reviews: {rev_status}")
            continue

        approval_count, has_rejection, approver_users = get_review_verdict(reviews)
        print(f"    Approvals: {approval_count}, Outstanding rejection: {has_rejection}")

        # Must meet approval threshold with no rejections
        if approval_count < REQUIRED_APPROVALS:
            print(f"    Skipping: needs {REQUIRED_APPROVALS} approvals, has {approval_count}")
            continue

        if has_rejection:
            print(f"    Skipping: has outstanding rejection")
            continue

        # Check protected paths
        files_status, changed_files = forgejo_get(
            client, f"/repos/{owner}/{repo}/pulls/{number}/files"
        )
        if files_status != 200:
            print(f"    Could not fetch changed files: {files_status}")
            continue

        if touches_protected_path(changed_files):
            if OPERATOR_USER not in approver_users:
                print(f"    Skipping: touches protected path, needs operator approval")
                continue
            print(f"    Protected path touched, operator approval present")

        # PR is eligible for merge
        if dry_run:
            print(f"    [DRY RUN] Would merge PR #{number}")
            results.append({
                "number": number,
                "title": title,
                "author": author,
                "action": "would_merge",
            })
            continue

        # Merge the PR — operator token has push whitelist access.
        # Approval enforcement is handled by merge_bot itself (above).
        # Forgejo branch protection no longer sets required_approvals.
        merge_status, merge_resp = forgejo_post(
            client, f"/repos/{owner}/{repo}/pulls/{number}/merge", {"Do": "merge"}
        )

        if merge_status in (200, 204):
            msg = f"PR #{number} '{title}' by {author} has been auto-merged to main."
            print(f"    Merged!")
            post_to_zulip(zulip_email, zulip_api_key, msg)
            results.append({
                "number": number,
                "title": title,
                "author": author,
                "action": "merged",
            })
        else:
            reason = merge_resp.get("message", str(merge_resp))
            msg = (
                f"PR #{number} '{title}' could not be auto-merged: "
                f"{reason}. Author must resolve."
            )
            print(f"    Merge failed: {reason}")
            post_to_zulip(zulip_email, zulip_api_key, msg)
            results.append({
                "number": number,
                "title": title,
                "author": author,
                "action": "failed",
                "reason": reason,
            })

    return results


def run_merge_bot(
    dry_run: bool = False,
    zulip_email: str | None = None,
    zulip_api_key: str | None = None,
) -> bool:
    """Main entry point: scan repos and merge eligible PRs."""
    token = get_forgejo_token()
    if not token:
        print("ERROR: No Forgejo token found.")
        print("Set FORGEJO_TOKEN env var or configure .claude/settings.local.json")
        return False

    if not zulip_email or not zulip_api_key:
        print("Warning: Zulip credentials not provided, notifications will be skipped.")

    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
    }

    all_results: list[dict] = []

    with httpx.Client(headers=headers, timeout=30) as client:
        for repo in APPROVED_REPOS:
            print(f"\n{'=' * 50}")
            print(f"Scanning {FORGEJO_ORG}/{repo}")
            print("=" * 50)

            results = process_repo(client, repo, dry_run, zulip_email, zulip_api_key)
            all_results.extend(results)

    # Summary
    merged = [r for r in all_results if r["action"] in ("merged", "would_merge")]
    failed = [r for r in all_results if r["action"] == "failed"]

    print(f"\n{'=' * 50}")
    print("MERGE BOT SUMMARY")
    print("=" * 50)
    if dry_run:
        print(f"  [DRY RUN] Would merge: {len(merged)} PRs")
    else:
        print(f"  Merged: {len(merged)} PRs")
    print(f"  Failed: {len(failed)} PRs")

    for r in merged:
        prefix = "[DRY RUN] " if dry_run else ""
        print(f"    {prefix}#{r['number']}: {r['title']} by {r['author']}")
    for r in failed:
        print(f"    FAILED #{r['number']}: {r['title']} - {r.get('reason', 'unknown')}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Auto-merge engine for City in a Bottle"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be merged without doing it",
    )
    parser.add_argument(
        "--zulip-email",
        default=os.environ.get("ZULIP_BOT_EMAIL"),
        help="Zulip bot email for notifications (or set ZULIP_BOT_EMAIL)",
    )
    parser.add_argument(
        "--zulip-api-key",
        default=os.environ.get("ZULIP_API_KEY"),
        help="Zulip bot API key (or set ZULIP_API_KEY)",
    )

    args = parser.parse_args()

    success = run_merge_bot(
        dry_run=args.dry_run,
        zulip_email=args.zulip_email,
        zulip_api_key=args.zulip_api_key,
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
