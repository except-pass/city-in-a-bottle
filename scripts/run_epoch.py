#!/usr/bin/env python3
"""
Run an epoch in the agent economy.

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
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "agent_economy")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "agent_economy")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "agent_economy_dev")

DEFAULT_FAUCET = int(os.environ.get("FAUCET_AMOUNT", "2000"))
DEFAULT_MAX_TURNS = int(os.environ.get("MAX_TURNS", "100"))

AGENTS_DIR = Path(__file__).parent.parent / ".data" / "agents"
REPO_DIR = Path(__file__).parent.parent


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


def rebuild_from_main(dry_run: bool = False) -> bool:
    """
    Rebuild from main branch.

    Returns True if rebuild was needed/successful.
    """
    print("\n=== Rebuilding from main ===")

    # Check for uncommitted changes
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=REPO_DIR
    )
    if result.stdout.strip():
        print("WARNING: Uncommitted changes in repo")
        print(result.stdout)

    # Pull latest
    # Pull from Forgejo (where agents submit PRs)
    # The 'forgejo' remote is set up by src/forgejo/setup.py
    print("Pulling latest from forgejo/main...")
    if not dry_run:
        # Check if forgejo remote exists
        result = subprocess.run(
            ["git", "remote", "get-url", "forgejo"],
            capture_output=True, text=True, cwd=REPO_DIR
        )
        if result.returncode != 0:
            print("  Note: 'forgejo' remote not configured, skipping pull")
            print("  Run setup-forgejo to enable agent PR workflow")
        else:
            result = subprocess.run(
                ["git", "pull", "forgejo", "main"],
                capture_output=True, text=True, cwd=REPO_DIR
            )
            if result.returncode != 0:
                print(f"  Git pull failed: {result.stderr}")
                # Don't abort - maybe nothing to pull yet
            else:
                print(result.stdout if result.stdout else "  Already up to date")

    # Rebuild containers
    print("Rebuilding containers...")
    if not dry_run:
        result = subprocess.run(
            ["docker", "compose", "build"],
            capture_output=True, text=True,
            cwd=REPO_DIR / "infra"
        )
        if result.returncode != 0:
            print(f"Docker build failed: {result.stderr}")
            # Don't fail - maybe nothing changed
        else:
            print("Containers rebuilt")

    # Restart services
    print("Restarting services...")
    if not dry_run:
        subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=REPO_DIR / "infra"
        )

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
        print(f"  {agent_id} failed: {result.stderr[-500:]}")
        return {"status": "failed", "error": result.stderr[-500:]}


def run_epoch(
    faucet_amount: int = DEFAULT_FAUCET,
    max_turns: int = DEFAULT_MAX_TURNS,
    dry_run: bool = False,
    agents: list[str] | None = None,
):
    """Run a complete epoch."""

    print("=" * 60)
    print("AGENT ECONOMY - EPOCH RUNNER")
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
    parser = argparse.ArgumentParser(description="Run an epoch in the agent economy")
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
