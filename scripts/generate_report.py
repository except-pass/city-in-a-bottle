#!/usr/bin/env python3
"""
Generate activity reports for City in a Bottle.

Creates readable summaries of what agents did, including:
- Token movements
- Actions taken (tool calls)
- Messages posted
- Jobs created/bid on
- Files written

Usage:
    python scripts/generate_report.py                    # Latest epoch
    python scripts/generate_report.py --epoch 5          # Specific epoch
    python scripts/generate_report.py --last-hours 24    # Last 24 hours
    python scripts/generate_report.py --save             # Save to .data/reports/
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2

# Config
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "agent_economy")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "agent_economy")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "agent_economy_dev")

REPORTS_DIR = Path(__file__).parent.parent / ".data" / "reports"


def get_db():
    return psycopg2.connect(
        host=POSTGRES_HOST, port=POSTGRES_PORT,
        dbname=POSTGRES_DB, user=POSTGRES_USER, password=POSTGRES_PASSWORD
    )


def get_epoch_info(conn, epoch_num=None):
    """Get epoch metadata."""
    cur = conn.cursor()
    if epoch_num:
        cur.execute("""
            SELECT epoch_number, started_at, ended_at, faucet_amount,
                   agents_run, total_faucet, git_commit, status
            FROM epochs WHERE epoch_number = %s
        """, (epoch_num,))
    else:
        cur.execute("""
            SELECT epoch_number, started_at, ended_at, faucet_amount,
                   agents_run, total_faucet, git_commit, status
            FROM epochs ORDER BY epoch_number DESC LIMIT 1
        """)
    return cur.fetchone()


def get_runs_for_epoch(conn, epoch_num):
    """Get all agent runs for an epoch."""
    cur = conn.cursor()
    # Get runs that happened during the epoch
    cur.execute("""
        SELECT r.run_id, r.agent_id, r.started_at, r.ended_at,
               r.tokens_in, r.tokens_out, r.actions, r.reasoning, r.status
        FROM agent_runs r
        JOIN epochs e ON r.started_at >= e.started_at
            AND (e.ended_at IS NULL OR r.started_at <= e.ended_at)
        WHERE e.epoch_number = %s
        ORDER BY r.started_at
    """, (epoch_num,))
    return cur.fetchall()


def get_recent_runs(conn, hours=24):
    """Get runs from the last N hours."""
    cur = conn.cursor()
    cur.execute("""
        SELECT run_id, agent_id, started_at, ended_at,
               tokens_in, tokens_out, actions, reasoning, status
        FROM agent_runs
        WHERE started_at > NOW() - INTERVAL '%s hours'
        ORDER BY started_at
    """, (hours,))
    return cur.fetchall()


def get_balances(conn):
    """Get current balances for all agents."""
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ON (agent_id) agent_id, balance_after
        FROM token_transactions
        ORDER BY agent_id, timestamp DESC
    """)
    return {row[0]: row[1] for row in cur.fetchall()}


def categorize_action(action):
    """Categorize an action for summary."""
    tool = action.get("tool", "unknown")

    if "ledger" in tool:
        return "ledger"
    elif "zulip" in tool or "board" in tool:
        if "send" in tool or "post" in tool:
            return "message_sent"
        elif "read" in tool or "list" in tool:
            return "message_read"
        else:
            return "messaging"
    elif "forgejo" in tool:
        return "git"
    elif tool in ("Read", "Write", "Edit"):
        return "file_io"
    else:
        return "other"


def summarize_actions(actions):
    """Create summary of actions taken."""
    if not actions:
        return {}

    summary = {}
    for action in actions:
        cat = categorize_action(action)
        summary[cat] = summary.get(cat, 0) + 1

    return summary


def extract_messages(actions):
    """Extract messages sent from actions."""
    messages = []
    for action in actions or []:
        tool = action.get("tool", "")
        inp = action.get("input", {})

        if "send_channel_message" in tool:
            messages.append({
                "type": "channel",
                "channel": inp.get("channel"),
                "topic": inp.get("topic"),
                "preview": (inp.get("content") or "")[:100]
            })
        elif "send_dm" in tool:
            messages.append({
                "type": "dm",
                "to": inp.get("recipients"),
                "preview": (inp.get("content") or "")[:100]
            })

    return messages


def extract_file_writes(actions):
    """Extract files written from actions."""
    files = []
    for action in actions or []:
        tool = action.get("tool", "")
        inp = action.get("input", {})

        if tool == "Write":
            files.append(inp.get("file_path", "unknown"))
        elif tool == "Edit":
            files.append(f"{inp.get('file_path', 'unknown')} (edited)")

    return files


def generate_report(conn, epoch_num=None, hours=None, verbose=True):
    """Generate a report."""
    lines = []

    # Header
    lines.append("=" * 70)
    lines.append("CITY IN A BOTTLE ACTIVITY REPORT")
    lines.append("=" * 70)
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # Get data
    if epoch_num or (not hours):
        epoch = get_epoch_info(conn, epoch_num)
        if epoch:
            epoch_num = epoch[0]
            lines.append(f"## Epoch {epoch_num}")
            lines.append(f"Started: {epoch[1]}")
            lines.append(f"Ended: {epoch[2] or 'ongoing'}")
            lines.append(f"Faucet: {epoch[3]:,} tokens/agent")
            lines.append(f"Agents run: {epoch[4]}")
            lines.append(f"Total distributed: {epoch[5]:,} tokens")
            lines.append(f"Git commit: {epoch[6]}")
            lines.append(f"Status: {epoch[7]}")
            lines.append("")
            runs = get_runs_for_epoch(conn, epoch_num)
        else:
            lines.append("No epochs found.")
            runs = []
    else:
        lines.append(f"## Last {hours} Hours")
        lines.append("")
        runs = get_recent_runs(conn, hours)

    # Current balances
    balances = get_balances(conn)
    lines.append("## Current Balances")
    lines.append("")
    for agent, balance in sorted(balances.items()):
        lines.append(f"  {agent}: {balance:,} tokens")
    lines.append("")

    # Per-agent summaries
    lines.append("## Agent Activity")
    lines.append("")

    for run in runs:
        run_id, agent_id, started, ended, tokens_in, tokens_out, actions, reasoning, status = run

        lines.append(f"### {agent_id}")
        lines.append(f"Run ID: {run_id}")
        lines.append(f"Status: {status}")
        lines.append(f"Tokens: {tokens_in} in / {tokens_out} out (spent: {tokens_out})")
        lines.append("")

        # Action summary
        if actions:
            summary = summarize_actions(actions)
            lines.append("**Actions:**")
            for cat, count in sorted(summary.items()):
                lines.append(f"  - {cat}: {count}")
            lines.append("")

        # Messages sent
        messages = extract_messages(actions)
        if messages:
            lines.append("**Messages sent:**")
            for msg in messages[:5]:  # Limit to 5
                if msg["type"] == "channel":
                    lines.append(f"  - #{msg['channel']}/{msg['topic']}: {msg['preview']}...")
                else:
                    lines.append(f"  - DM to {msg['to']}: {msg['preview']}...")
            if len(messages) > 5:
                lines.append(f"  ... and {len(messages) - 5} more")
            lines.append("")

        # Files written
        files = extract_file_writes(actions)
        if files:
            lines.append("**Files written:**")
            for f in files[:5]:
                lines.append(f"  - {f}")
            if len(files) > 5:
                lines.append(f"  ... and {len(files) - 5} more")
            lines.append("")

        # Reasoning (truncated)
        if reasoning and verbose:
            lines.append("**Reasoning:**")
            lines.append(f"  {reasoning[:200]}...")
            lines.append("")

        lines.append("-" * 40)
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate City in a Bottle reports")
    parser.add_argument("--epoch", type=int, help="Specific epoch number")
    parser.add_argument("--last-hours", type=int, help="Report on last N hours")
    parser.add_argument("--save", action="store_true", help="Save to .data/reports/")
    parser.add_argument("--quiet", action="store_true", help="Less verbose output")

    args = parser.parse_args()

    conn = get_db()
    report = generate_report(
        conn,
        epoch_num=args.epoch,
        hours=args.last_hours,
        verbose=not args.quiet
    )

    print(report)

    if args.save:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        if args.epoch:
            filename = f"epoch_{args.epoch}.md"
        else:
            filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

        path = REPORTS_DIR / filename
        path.write_text(report)
        print(f"\nSaved to: {path}")

    conn.close()


if __name__ == "__main__":
    main()
