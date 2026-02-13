#!/usr/bin/env python3
"""
Run an epoch in City in a Bottle.

An epoch is a cycle where:
1. Code is rebuilt from main (if changed)
2. All agents receive faucet tokens
3. All agents run their turns (up to max_turns)
4. Results are logged

Usage:
    python scripts/run_epoch.py
    python scripts/run_epoch.py --faucet 2000 --max-turns 100
    python scripts/run_epoch.py --dry-run  # Show what would happen

Environment:
    POSTGRES_* - Database connection
    FAUCET_AMOUNT - Default faucet (or use --faucet)
    MAX_TURNS - Default max turns (or use --max-turns)
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import psycopg2

# Configuration
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5434")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "agent_economy")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "agent_economy")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "agent_economy_dev")

DEFAULT_FAUCET = int(os.environ.get("FAUCET_AMOUNT", "2000"))
DEFAULT_MAX_TURNS = int(os.environ.get("MAX_TURNS", "25"))

AGENTS_DIR = Path(__file__).parent.parent / ".data" / "agents"
REPO_DIR = Path(__file__).parent.parent

# Forgejo config (for authenticated pull)
FORGEJO_URL = os.environ.get("FORGEJO_URL", f"http://localhost:{os.environ.get('FORGEJO_PORT', '3300')}")
FORGEJO_ORG = os.environ.get("FORGEJO_ORG", "workspace")
FORGEJO_REPO = os.environ.get("FORGEJO_REPO", "agent-economy")


def get_db_connection():
    """Get database connection."""
    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )


def get_current_epoch(conn) -> int:
    """Get the current (latest) epoch number, or 0 if none."""
    cur = conn.cursor()
    cur.execute("SELECT MAX(epoch_number) FROM epochs")
    row = cur.fetchone()
    return row[0] if row[0] is not None else 0


def get_git_commit() -> str:
    """Get current git commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=REPO_DIR
        )
        return result.stdout.strip()[:12]
    except Exception:
        return "unknown"


def get_forgejo_token() -> str | None:
    """Get operator Forgejo token from env or .claude/settings.local.json."""
    # Check environment first
    token = os.environ.get("FORGEJO_TOKEN")
    if token:
        return token

    # Fall back to .claude/settings.local.json
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


def rebuild_from_main(dry_run: bool = False) -> bool:
    """
    Rebuild from main branch.

    Returns True if rebuild was needed/successful.
    """
    print("\n=== Rebuilding from main ===")

    # Snapshot current HEAD to detect changes
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=REPO_DIR
    ).stdout.strip()

    # Check for uncommitted changes
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=REPO_DIR
    )
    if result.stdout.strip():
        print("WARNING: Uncommitted changes in repo")
        print(result.stdout)

    # Pull latest from Forgejo (where agents submit PRs)
    # Uses authenticated URL so token stays in memory, never in .git/config
    print("Pulling latest from forgejo/main...")
    code_changed = False
    if not dry_run:
        token = get_forgejo_token()
        if not token:
            print("  Note: No Forgejo token found, skipping pull")
            print("  Set FORGEJO_TOKEN env var or run setup-forgejo")
        else:
            forgejo_host = FORGEJO_URL.replace("http://", "")
            auth_url = f"http://operator:{token}@{forgejo_host}/{FORGEJO_ORG}/{FORGEJO_REPO}.git"
            result = subprocess.run(
                ["git", "pull", auth_url, "main"],
                capture_output=True, text=True, cwd=REPO_DIR
            )
            if result.returncode != 0:
                print(f"  Git pull failed: {result.stderr}")
            else:
                print(result.stdout if result.stdout else "  Already up to date")

        # Check if HEAD moved
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=REPO_DIR
        ).stdout.strip()
        code_changed = head_before != head_after

    if code_changed:
        print("Code changed, rebuilding and restarting...")
        subprocess.run(
            ["docker", "compose", "--profile", "agent", "build"],
            capture_output=True, text=True,
            cwd=REPO_DIR / "infra"
        )
        subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=REPO_DIR / "infra"
        )
        print("Containers rebuilt and services restarted")
    else:
        print("No code changes, skipping rebuild")

    return True


def get_all_agents() -> list[str]:
    """Get list of all registered agent IDs."""
    if not AGENTS_DIR.exists():
        return []

    agents = []
    for agent_dir in AGENTS_DIR.iterdir():
        if agent_dir.is_dir() and (agent_dir / "config.json").exists():
            agents.append(agent_dir.name)

    return sorted(agents)


def get_agent_balance(conn, agent_id: str) -> int:
    """Get current balance for an agent."""
    cur = conn.cursor()
    cur.execute("""
        SELECT balance_after FROM token_transactions
        WHERE agent_id = %s ORDER BY timestamp DESC LIMIT 1
    """, (agent_id,))
    row = cur.fetchone()
    return row[0] if row else 0


def credit_faucet(conn, agent_id: str, amount: int, epoch: int) -> int:
    """Credit faucet tokens to an agent. Returns new balance."""
    cur = conn.cursor()

    # Get current balance
    current_balance = get_agent_balance(conn, agent_id)
    new_balance = current_balance + amount

    # Insert transaction
    cur.execute("""
        INSERT INTO token_transactions
            (agent_id, tx_type, amount, balance_after, reason, note)
        VALUES (%s, 'credit', %s, %s, 'faucet', %s)
    """, (agent_id, amount, new_balance, f"Epoch {epoch} faucet"))

    conn.commit()
    return new_balance


def run_agent(agent_id: str, max_turns: int, epoch: int, dry_run: bool = False) -> dict:
    """
    Run an agent for up to max_turns.

    Note: max_turns is read from config.json, not command line.
    The parameter here is just for display/logging.

    Returns dict with status and details.
    """
    print(f"\n--- Running {agent_id} (max {max_turns} turns) ---")

    if dry_run:
        return {"status": "skipped", "reason": "dry_run"}

    # Set epoch in environment so agent can see it
    env = os.environ.copy()
    env["EPOCH_NUMBER"] = str(epoch)

    # Run the agent using run-agent.sh
    # max_turns comes from agent's config.json, not CLI
    result = subprocess.run(
        ["./run-agent.sh", agent_id],
        capture_output=True, text=True,
        cwd=REPO_DIR,
        env=env,
        timeout=1800,  # 30 min timeout per agent
    )

    if result.returncode == 0:
        print(f"  {agent_id} completed successfully")
        return {"status": "completed", "output": result.stdout[-1000:]}  # Last 1000 chars
    else:
        combined = (result.stdout[-500:] + "\n" + result.stderr[-500:]).strip()
        print(f"  {agent_id} failed (exit {result.returncode}):\n{combined}")
        return {"status": "failed", "error": combined}


def run_epoch(
    faucet_amount: int = DEFAULT_FAUCET,
    max_turns: int = DEFAULT_MAX_TURNS,
    dry_run: bool = False,
    agents: list[str] | None = None,
):
    """Run a complete epoch."""

    print("=" * 60)
    print("CITY IN A BOTTLE - EPOCH RUNNER")
    print("=" * 60)

    if dry_run:
        print("*** DRY RUN - No changes will be made ***")

    # Connect to database
    conn = get_db_connection()

    # Determine epoch number
    current_epoch = get_current_epoch(conn)
    new_epoch = current_epoch + 1

    print(f"\nStarting Epoch {new_epoch}")
    print(f"  Faucet: {faucet_amount:,} tokens per agent")
    print(f"  Max turns: {max_turns}")

    # Rebuild from main
    if not rebuild_from_main(dry_run):
        print("ERROR: Rebuild failed, aborting epoch")
        return False

    # Get git commit
    git_commit = get_git_commit()
    print(f"  Git commit: {git_commit}")

    # Get agents to run
    if agents is None:
        agents = get_all_agents()

    if not agents:
        print("ERROR: No agents found")
        return False

    print(f"\nAgents to run: {', '.join(agents)}")

    # Create epoch record
    if not dry_run:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO epochs (epoch_number, faucet_amount, max_turns, git_commit, status)
            VALUES (%s, %s, %s, %s, 'running')
        """, (new_epoch, faucet_amount, max_turns, git_commit))
        conn.commit()

    # Process each agent
    total_faucet = 0
    agents_completed = 0

    for agent_id in agents:
        print(f"\n=== Agent: {agent_id} ===")

        # Get balance before
        balance_before = get_agent_balance(conn, agent_id)
        print(f"  Balance before: {balance_before:,}")

        # Credit faucet
        if not dry_run:
            new_balance = credit_faucet(conn, agent_id, faucet_amount, new_epoch)
            print(f"  Faucet credited: +{faucet_amount:,} -> {new_balance:,}")
            total_faucet += faucet_amount

            # Record participation
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO epoch_participation
                    (epoch_number, agent_id, faucet_received, balance_before, status)
                VALUES (%s, %s, %s, %s, 'running')
            """, (new_epoch, agent_id, faucet_amount, balance_before))
            conn.commit()
        else:
            print(f"  [DRY RUN] Would credit {faucet_amount:,} tokens")

        # Run the agent
        result = run_agent(agent_id, max_turns, new_epoch, dry_run)

        # Update participation record
        if not dry_run:
            balance_after = get_agent_balance(conn, agent_id)
            cur = conn.cursor()
            cur.execute("""
                UPDATE epoch_participation
                SET status = %s, balance_after = %s, ended_at = NOW(),
                    error_message = %s
                WHERE epoch_number = %s AND agent_id = %s
            """, (
                result["status"],
                balance_after,
                result.get("error"),
                new_epoch,
                agent_id
            ))
            conn.commit()

            if result["status"] == "completed":
                agents_completed += 1

    # Finalize epoch
    if not dry_run:
        cur = conn.cursor()
        cur.execute("""
            UPDATE epochs
            SET status = 'completed', ended_at = NOW(),
                agents_run = %s, total_faucet = %s
            WHERE epoch_number = %s
        """, (agents_completed, total_faucet, new_epoch))
        conn.commit()

    # Summary
    print("\n" + "=" * 60)
    print(f"EPOCH {new_epoch} COMPLETE")
    print("=" * 60)
    print(f"  Agents run: {agents_completed}/{len(agents)}")
    print(f"  Total faucet distributed: {total_faucet:,} tokens")

    # Show final balances
    print("\nFinal balances:")
    for agent_id in agents:
        balance = get_agent_balance(conn, agent_id)
        print(f"  {agent_id}: {balance:,}")

    # Per-agent activity summary
    if not dry_run:
        try:
            from generate_report import (
                get_runs_for_epoch, summarize_actions,
                extract_messages, extract_file_writes,
            )
            runs = get_runs_for_epoch(conn, new_epoch)
            if runs:
                print("\nAgent activity:")
                for run in runs:
                    run_id, agent_id, started, ended, tokens_in, tokens_out, actions, reasoning, status = run
                    print(f"\n  {agent_id} ({status}, spent {tokens_out:,} tokens):")

                    # Messages sent
                    messages = extract_messages(actions)
                    for msg in messages[:3]:
                        if msg["type"] == "channel":
                            print(f"    -> #{msg['channel']}/{msg['topic']}: {msg['preview'][:60]}")
                        else:
                            print(f"    -> DM to {msg['to']}: {msg['preview'][:60]}")

                    # Files written
                    files = extract_file_writes(actions)
                    for f in files[:3]:
                        print(f"    -> wrote {f}")

                    # Action counts
                    summary = summarize_actions(actions)
                    counts = ", ".join(f"{k}:{v}" for k, v in sorted(summary.items()))
                    if counts:
                        print(f"    [{counts}]")

                    # Reasoning snippet
                    if reasoning:
                        snippet = reasoning.replace("\n", " ")[:100].strip()
                        print(f"    \"{snippet}...\"")
        except Exception as e:
            print(f"\nWarning: Could not summarize activity: {e}")

    # Generate and save report
    if not dry_run:
        try:
            from generate_report import generate_report as gen_report, REPORTS_DIR
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            report = gen_report(conn, epoch_num=new_epoch, verbose=True)
            report_path = REPORTS_DIR / f"epoch_{new_epoch}.md"
            report_path.write_text(report)
            print(f"\nReport saved to: {report_path}")
        except Exception as e:
            print(f"\nWarning: Could not generate report: {e}")

    conn.close()
    return True


def main():
    parser = argparse.ArgumentParser(description="Run an epoch in City in a Bottle")
    parser.add_argument("--faucet", type=int, default=DEFAULT_FAUCET,
                        help=f"Tokens to give each agent (default: {DEFAULT_FAUCET})")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS,
                        help=f"Max turns per agent (default: {DEFAULT_MAX_TURNS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without making changes")
    parser.add_argument("--agents", nargs="+",
                        help="Specific agents to run (default: all)")

    args = parser.parse_args()

    success = run_epoch(
        faucet_amount=args.faucet,
        max_turns=args.max_turns,
        dry_run=args.dry_run,
        agents=args.agents,
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
