#!/usr/bin/env python3
"""
Verify what the test agent accomplished.

Checks:
1. Zulip: Messages posted to #system channel
2. Forgejo: Repos created, PRs opened, issues filed
3. Ledger: Token balance and transactions
4. Files: Test progress log in agent directory

Usage:
    python scripts/verify_agent_tests.py
    python scripts/verify_agent_tests.py --agent agent_tester
"""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Config
ZULIP_URL = os.environ.get("ZULIP_URL", "https://localhost:8443")
FORGEJO_URL = os.environ.get("FORGEJO_URL", f"http://localhost:{os.environ.get('FORGEJO_PORT', '3300')}")
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5434")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "agent_economy")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "agent_economy")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "agent_economy_dev")

AGENTS_DIR = Path(__file__).parent.parent / ".data" / "agents"


class TestVerifier:
    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self.agent_dir = AGENTS_DIR / agent_name
        self.results = {"passed": [], "failed": [], "info": []}

        # Load agent config
        config_path = self.agent_dir / "config.json"
        if config_path.exists():
            self.config = json.loads(config_path.read_text())
        else:
            self.config = {}

        # Load Zulip credentials
        zuliprc_path = self.agent_dir / ".zuliprc"
        self.zulip_creds = self._parse_zuliprc(zuliprc_path) if zuliprc_path.exists() else {}

    def _parse_zuliprc(self, path: Path) -> dict:
        """Parse .zuliprc file."""
        creds = {}
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("["):
                key, val = line.split("=", 1)
                creds[key.strip()] = val.strip()
        return creds

    def _pass(self, test: str, detail: str = ""):
        self.results["passed"].append(f"✅ {test}" + (f": {detail}" if detail else ""))

    def _fail(self, test: str, detail: str = ""):
        self.results["failed"].append(f"❌ {test}" + (f": {detail}" if detail else ""))

    def _info(self, msg: str):
        self.results["info"].append(f"ℹ️  {msg}")

    # =========================================================================
    # LEDGER CHECKS
    # =========================================================================

    def check_ledger(self):
        """Check agent's ledger balance and transactions."""
        print("\n== Ledger Checks ==")

        try:
            conn = psycopg2.connect(
                host=POSTGRES_HOST, port=POSTGRES_PORT,
                dbname=POSTGRES_DB, user=POSTGRES_USER, password=POSTGRES_PASSWORD
            )
            cur = conn.cursor()

            # Get current balance
            cur.execute("""
                SELECT balance_after FROM token_transactions
                WHERE agent_id = %s ORDER BY timestamp DESC LIMIT 1
            """, (self.agent_name,))
            row = cur.fetchone()

            if row:
                balance = row[0]
                self._pass("Has balance", f"{balance} tokens")
            else:
                self._fail("Has balance", "No transactions found")
                return

            # Get transaction count
            cur.execute("""
                SELECT COUNT(*) FROM token_transactions WHERE agent_id = %s
            """, (self.agent_name,))
            tx_count = cur.fetchone()[0]
            self._info(f"Total transactions: {tx_count}")

            # Check for run debits (agent actually ran)
            cur.execute("""
                SELECT COUNT(*) FROM token_transactions
                WHERE agent_id = %s AND tx_type = 'debit' AND reason = 'run_cost'
            """, (self.agent_name,))
            run_count = cur.fetchone()[0]

            if run_count > 0:
                self._pass("Agent ran", f"{run_count} runs recorded")
            else:
                self._fail("Agent ran", "No run costs recorded")

            conn.close()

        except Exception as e:
            self._fail("Ledger connection", str(e))

    # =========================================================================
    # ZULIP CHECKS
    # =========================================================================

    def check_zulip(self):
        """Check Zulip messages from the agent."""
        print("\n== Zulip Checks ==")

        if not self.zulip_creds:
            self._fail("Zulip credentials", "No .zuliprc found")
            return

        email = self.zulip_creds.get("email")
        key = self.zulip_creds.get("key")

        if not email or not key:
            self._fail("Zulip credentials", "Missing email or key")
            return

        # Test authentication
        try:
            resp = requests.get(
                f"{ZULIP_URL}/api/v1/users/me",
                auth=(email, key),
                verify=False, timeout=10
            )
            if resp.status_code == 200:
                user_info = resp.json()
                self._pass("Zulip auth", f"Authenticated as {user_info.get('full_name')}")
            else:
                self._fail("Zulip auth", f"Status {resp.status_code}: {resp.text[:100]}")
                return
        except Exception as e:
            self._fail("Zulip auth", str(e))
            return

        # Check for messages in #system channel
        try:
            # Get user ID first
            user_id = user_info.get("user_id")

            # Search for messages from this user
            resp = requests.get(
                f"{ZULIP_URL}/api/v1/messages",
                auth=(email, key),
                params={
                    "anchor": "newest",
                    "num_before": 100,
                    "num_after": 0,
                    "narrow": json.dumps([
                        {"operator": "stream", "operand": "system"},
                        {"operator": "sender", "operand": email}
                    ])
                },
                verify=False, timeout=10
            )

            if resp.status_code == 200:
                messages = resp.json().get("messages", [])
                if messages:
                    self._pass("Posted to #system", f"{len(messages)} messages")
                    # Show recent messages
                    for msg in messages[-3:]:
                        content = msg.get("content", "")[:80]
                        self._info(f"Message: {content}...")
                else:
                    self._fail("Posted to #system", "No messages found")
            else:
                self._fail("Check messages", f"Status {resp.status_code}")

        except Exception as e:
            self._fail("Check messages", str(e))

    # =========================================================================
    # FORGEJO CHECKS
    # =========================================================================

    def check_forgejo(self):
        """Check Forgejo repos, PRs, issues from the agent."""
        print("\n== Forgejo Checks ==")

        forgejo_config = self.config.get("forgejo", {})
        token = forgejo_config.get("token")
        username = forgejo_config.get("username", self.agent_name)

        if not token:
            self._fail("Forgejo credentials", "No token in config")
            return

        headers = {"Authorization": f"token {token}"}

        # Check authentication
        try:
            resp = requests.get(
                f"{FORGEJO_URL}/api/v1/user",
                headers=headers, timeout=10
            )
            if resp.status_code == 200:
                user_info = resp.json()
                self._pass("Forgejo auth", f"Authenticated as {user_info.get('login')}")
            else:
                self._fail("Forgejo auth", f"Status {resp.status_code}")
                return
        except Exception as e:
            self._fail("Forgejo auth", str(e))
            return

        # Check for repos created by agent
        try:
            resp = requests.get(
                f"{FORGEJO_URL}/api/v1/user/repos",
                headers=headers, timeout=10
            )
            if resp.status_code == 200:
                repos = resp.json()
                if repos:
                    self._pass("Created repos", f"{len(repos)} repos")
                    for repo in repos[:3]:
                        self._info(f"Repo: {repo.get('full_name')}")
                else:
                    self._info("No repos created (might be expected)")
        except Exception as e:
            self._fail("List repos", str(e))

        # Check for PRs opened by agent in workspace/agent-contributions
        try:
            resp = requests.get(
                f"{FORGEJO_URL}/api/v1/repos/workspace/agent-contributions/pulls",
                headers=headers,
                params={"state": "all"},
                timeout=10
            )
            if resp.status_code == 200:
                prs = resp.json()
                agent_prs = [pr for pr in prs if pr.get("user", {}).get("login") == username]
                if agent_prs:
                    self._pass("Opened PRs", f"{len(agent_prs)} PRs in agent-contributions")
                    for pr in agent_prs[:3]:
                        self._info(f"PR #{pr.get('number')}: {pr.get('title')}")
                else:
                    self._info("No PRs opened (might be expected for early tests)")
            elif resp.status_code == 404:
                self._info("workspace/agent-contributions repo not found")
        except Exception as e:
            self._fail("List PRs", str(e))

        # Check for issues created by agent
        try:
            resp = requests.get(
                f"{FORGEJO_URL}/api/v1/repos/workspace/agent-contributions/issues",
                headers=headers,
                params={"state": "all", "created_by": username},
                timeout=10
            )
            if resp.status_code == 200:
                issues = resp.json()
                if issues:
                    self._pass("Created issues", f"{len(issues)} issues")
                    for issue in issues[:3]:
                        self._info(f"Issue #{issue.get('number')}: {issue.get('title')}")
                else:
                    self._info("No issues created (might be expected)")
        except Exception as e:
            self._fail("List issues", str(e))

    # =========================================================================
    # FILE CHECKS
    # =========================================================================

    def check_files(self):
        """Check files created by the agent."""
        print("\n== File Checks ==")

        # Check for test progress file
        progress_file = self.agent_dir / "memories" / "test_progress.md"
        if progress_file.exists():
            content = progress_file.read_text()
            self._pass("Test progress file", f"{len(content)} bytes")

            # Count completed phases
            completed = content.count("✅ COMPLETE") + content.count("✅ COMPLETED")
            self._info(f"Completed phases: {completed}")

            # Check for bugs found
            if "BUG" in content.upper() or "FAIL" in content:
                self._info("Bugs/failures noted in progress file")
        else:
            self._fail("Test progress file", "Not found")

        # Check memories directory
        memories_dir = self.agent_dir / "memories"
        if memories_dir.exists():
            files = list(memories_dir.glob("*.md"))
            self._info(f"Memory files: {len(files)}")

    # =========================================================================
    # RUN ALL
    # =========================================================================

    def run_all(self):
        """Run all verification checks."""
        print(f"\n{'='*60}")
        print(f"Verifying agent: {self.agent_name}")
        print(f"{'='*60}")

        self.check_ledger()
        self.check_zulip()
        self.check_forgejo()
        self.check_files()

        # Summary
        print(f"\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")

        for item in self.results["passed"]:
            print(item)
        for item in self.results["failed"]:
            print(item)
        print()
        for item in self.results["info"]:
            print(item)

        passed = len(self.results["passed"])
        failed = len(self.results["failed"])
        print(f"\nTotal: {passed} passed, {failed} failed")

        return failed == 0


def main():
    parser = argparse.ArgumentParser(description="Verify agent test results")
    parser.add_argument("--agent", default="agent_tester", help="Agent name to verify")
    args = parser.parse_args()

    verifier = TestVerifier(args.agent)
    success = verifier.run_all()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
