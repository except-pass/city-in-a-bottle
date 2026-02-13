#!/usr/bin/env python3
"""
Create a new agent in the economy.

Sets up:
1. Agent directory with config.json and agent.md
2. Database entry with initial token balance
3. Zulip bot account for messaging
4. (Optional) Forgejo account for git access

Usage:
    python scripts/create_agent.py agent_name
    python scripts/create_agent.py agent_name --endowment 50000
    python scripts/create_agent.py agent_name --personality "A cautious trader who values long-term relationships"

Example:
    python scripts/create_agent.py agent_beta --endowment 100000
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg2
import requests
import urllib3

# Disable SSL warnings for self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Defaults
DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_ENDOWMENT = 100000
DEFAULT_TICK_INTERVAL = 300
DEFAULT_MAX_TURNS = 10

# Zulip config
ZULIP_URL = os.environ.get("ZULIP_URL", "https://localhost:8443")
ZULIP_ADMIN_EMAIL = "admin@agent-economy.local"
ZULIP_ADMIN_PASSWORD = "admin-dev-password-123"

# Forgejo config
FORGEJO_URL = os.environ.get("FORGEJO_URL", f"http://localhost:{os.environ.get('FORGEJO_PORT', '3300')}")
# Container-facing URL for agent configs (Docker DNS, not localhost)
AGENT_FORGEJO_URL = os.environ.get("AGENT_FORGEJO_URL", "http://forgejo:3000")
FORGEJO_CONTAINER = "agent_economy_forgejo"

# Postgres config
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5434")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "agent_economy")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "agent_economy")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "agent_economy_dev")


def get_zulip_admin_api_key() -> str | None:
    """Get admin API key from Zulip container."""
    result = subprocess.run(
        [
            "docker", "exec", "-u", "zulip", "agent_economy_zulip",
            "/home/zulip/deployments/current/manage.py", "shell", "-c",
            f"from zerver.models import UserProfile; u = UserProfile.objects.get(delivery_email='{ZULIP_ADMIN_EMAIL}'); print(u.api_key)"
        ],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        api_key = result.stdout.strip().split('\n')[-1]
        if len(api_key) == 32:
            return api_key
    return None


def create_zulip_bot(agent_name: str, admin_api_key: str) -> dict | None:
    """Create a Zulip bot for the agent."""
    bot_name = agent_name.replace("_", "-")
    full_name = agent_name.replace("_", " ").title()

    resp = requests.post(
        f"{ZULIP_URL}/api/v1/bots",
        auth=(ZULIP_ADMIN_EMAIL, admin_api_key),
        data={
            "short_name": bot_name,
            "full_name": full_name,
            "bot_type": 1,
        },
        verify=False
    )

    if resp.status_code == 200:
        result = resp.json()
        if result.get("result") == "success":
            return {
                "user_id": result["user_id"],
                "api_key": result["api_key"],
                "email": f"{bot_name}-bot@localhost",
            }
    elif resp.status_code == 400 and "already in use" in resp.text.lower():
        # Bot exists, get its info
        resp = requests.get(
            f"{ZULIP_URL}/api/v1/bots",
            auth=(ZULIP_ADMIN_EMAIL, admin_api_key),
            verify=False
        )
        if resp.status_code == 200:
            for bot in resp.json().get("bots", []):
                bot_email = bot.get("email") or bot.get("username", "")
                if f"{bot_name}-bot" in bot_email:
                    return {
                        "user_id": bot.get("user_id"),
                        "api_key": bot["api_key"],
                        "email": bot_email,
                    }

    print(f"  Warning: Could not create Zulip bot: {resp.text}")
    return None


def subscribe_bot_to_channels(bot_email: str, api_key: str) -> bool:
    """Subscribe bot to system channels."""
    channels = [
        {"name": "job-board"},
        {"name": "results"},
        {"name": "system"},
    ]

    resp = requests.post(
        f"{ZULIP_URL}/api/v1/users/me/subscriptions",
        auth=(bot_email, api_key),
        data={"subscriptions": json.dumps(channels)},
        verify=False
    )
    return resp.status_code == 200


def create_forgejo_user(agent_name: str) -> dict | None:
    """Create a Forgejo user for the agent with an access token.

    Returns dict with username and token on success, None on failure.
    """
    username = agent_name
    email = f"{agent_name}@agent.economy"
    password = f"{agent_name}-dev-password"  # Simple password for dev

    # Check if user already exists
    result = subprocess.run(
        [
            "docker", "exec", "-u", "git", FORGEJO_CONTAINER,
            "forgejo", "admin", "user", "list"
        ],
        capture_output=True, text=True
    )

    if result.returncode == 0 and username in result.stdout:
        print(f"  Forgejo user {username} already exists")
        # Try to generate a new token for existing user
        token_result = subprocess.run(
            [
                "docker", "exec", "-u", "git", FORGEJO_CONTAINER,
                "forgejo", "admin", "user", "generate-access-token",
                "--username", username,
                "--token-name", f"{username}-agent-token",
                "--scopes", "all"
            ],
            capture_output=True, text=True
        )
        if token_result.returncode == 0:
            # Parse token from output (format: "Access token was successfully created: <token>")
            for line in token_result.stdout.strip().split('\n'):
                if 'successfully created' in line.lower() or len(line) == 40:
                    token = line.split(':')[-1].strip() if ':' in line else line.strip()
                    if len(token) >= 20:
                        return {"username": username, "token": token}
        print(f"  Warning: Could not generate token for existing user")
        return None

    # Create new user with access token
    result = subprocess.run(
        [
            "docker", "exec", "-u", "git", FORGEJO_CONTAINER,
            "forgejo", "admin", "user", "create",
            "--username", username,
            "--email", email,
            "--password", password,
            "--access-token",
            "--must-change-password=false"
        ],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"  Forgejo error: {result.stderr}")
        return None

    # Parse token from output
    # Output format: "New user 'agent_x' has been successfully created!\nAccess token was successfully created... <token>"
    token = None
    for line in result.stdout.strip().split('\n'):
        line = line.strip()
        # Token is typically 40 chars hex
        if len(line) == 40 and line.isalnum():
            token = line
            break
        if 'access token' in line.lower() and ':' in line:
            token = line.split(':')[-1].strip()
            break

    if not token:
        # Try to find token in output
        import re
        match = re.search(r'[a-f0-9]{40}', result.stdout)
        if match:
            token = match.group()

    if token:
        return {"username": username, "token": token}

    print(f"  Warning: User created but could not parse token from output")
    print(f"  Output: {result.stdout}")
    return None


def create_database_entry(agent_id: str, initial_balance: int) -> bool:
    """Create agent entry in the database with initial token balance.

    The schema tracks balance via token_transactions - balance is derived
    from the most recent transaction's balance_after field.
    """
    try:
        conn = psycopg2.connect(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            dbname=POSTGRES_DB,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
        )
        cur = conn.cursor()

        # Check if agent already has transactions (already exists)
        cur.execute(
            "SELECT balance_after FROM token_transactions WHERE agent_id = %s ORDER BY timestamp DESC LIMIT 1",
            (agent_id,)
        )
        row = cur.fetchone()
        if row:
            print(f"  Agent {agent_id} already exists with balance {row[0]}")
            conn.close()
            return True

        # Create initial endowment transaction
        cur.execute(
            """INSERT INTO token_transactions
               (agent_id, counterparty_id, tx_type, amount, balance_after, reason, note)
               VALUES (%s, 'system', 'credit', %s, %s, 'initial_endowment', 'New agent created')""",
            (agent_id, initial_balance, initial_balance)
        )

        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"  Database error: {e}")
        return False


def create_agent_directory(
    agents_dir: Path,
    agent_name: str,
    model: str,
    endowment: int,
    tick_interval: int,
    max_turns: int,
    personality: str | None,
) -> Path:
    """Create the agent directory structure."""
    agent_dir = agents_dir / agent_name

    if agent_dir.exists():
        print(f"  Directory {agent_dir} already exists")
        return agent_dir

    # Create directories
    agent_dir.mkdir(parents=True)
    (agent_dir / "skills").mkdir()
    (agent_dir / "memories").mkdir()

    # Create config.json (forgejo config added later)
    config = {
        "model": model,
        "tick_interval_seconds": tick_interval,
        "initial_endowment": endowment,
        "max_turns": max_turns,
        "debt_limit": None,
    }
    (agent_dir / "config.json").write_text(json.dumps(config, indent=4) + "\n")

    # Create agent.md
    display_name = agent_name.replace("_", " ").title().replace("Agent ", "")
    if personality:
        personality_section = personality
    else:
        personality_section = "A new agent in the economy. Still figuring things out."

    agent_md = f"""# {display_name}

{personality_section}

## Starting Strategy: OODA Loop

If you're not sure what to do, a good default is the OODA loop:

1. **Observe** - Check the message board, your balance, what's happening
2. **Orient** - Understand your situation, opportunities, threats
3. **Decide** - Pick an action: bid on work, complete a job, explore, wait
4. **Act** - Execute efficiently (remember: output costs tokens!)

You can `poll_for_updates()` to observe the message board in real-time and react to new opportunities as they appear.

## Notes

- This file is loaded every run, so keep it concise (longer = more tokens burned)
- Save notes and learnings to `memories/` — organize however you want
- Build reusable templates in `skills/` to reduce future costs
- You can edit this file to evolve your strategy

---
*This is who I am. I will refine this as I learn.*
"""
    (agent_dir / "agent.md").write_text(agent_md)

    # Create .gitkeep in directories
    (agent_dir / "memories" / ".gitkeep").write_text("")
    (agent_dir / "skills" / ".gitkeep").write_text("")

    return agent_dir


def save_zulip_credentials(agent_dir: Path, bot_info: dict) -> None:
    """Save Zulip bot credentials to .zuliprc."""
    zuliprc = f"""[api]
email={bot_info['email']}
key={bot_info['api_key']}
site={ZULIP_URL}
"""
    (agent_dir / ".zuliprc").write_text(zuliprc)


def update_config_with_forgejo(agent_dir: Path, forgejo_info: dict) -> None:
    """Update config.json with Forgejo credentials."""
    config_path = agent_dir / "config.json"
    config = json.loads(config_path.read_text())
    config["forgejo"] = {
        "url": AGENT_FORGEJO_URL,
        "username": forgejo_info["username"],
        "token": forgejo_info["token"],
    }
    config_path.write_text(json.dumps(config, indent=4) + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Create a new agent in the economy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s agent_beta
    %(prog)s agent_gamma --endowment 50000
    %(prog)s agent_delta --personality "A risk-taking speculator"
        """
    )
    parser.add_argument("agent_name", help="Agent identifier (e.g., agent_beta)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model to use (default: {DEFAULT_MODEL})")
    parser.add_argument("--endowment", type=int, default=DEFAULT_ENDOWMENT, help=f"Initial token balance (default: {DEFAULT_ENDOWMENT})")
    parser.add_argument("--tick-interval", type=int, default=DEFAULT_TICK_INTERVAL, help=f"Seconds between runs (default: {DEFAULT_TICK_INTERVAL})")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS, help=f"Max turns per run (default: {DEFAULT_MAX_TURNS})")
    parser.add_argument("--personality", help="Brief personality description for agent.md")
    parser.add_argument("--agents-dir", type=Path, default=Path(__file__).parent.parent / ".data" / "agents", help="Agents directory")
    parser.add_argument("--skip-zulip", action="store_true", help="Skip Zulip bot creation")
    parser.add_argument("--skip-forgejo", action="store_true", help="Skip Forgejo user creation")
    parser.add_argument("--skip-db", action="store_true", help="Skip database entry")

    args = parser.parse_args()

    # Validate agent name
    if not args.agent_name.replace("_", "").isalnum():
        print(f"Error: Agent name must be alphanumeric with underscores only")
        sys.exit(1)

    print(f"Creating agent: {args.agent_name}")

    # 1. Create directory structure
    print("1. Creating agent directory...")
    agent_dir = create_agent_directory(
        args.agents_dir,
        args.agent_name,
        args.model,
        args.endowment,
        args.tick_interval,
        args.max_turns,
        args.personality,
    )
    print(f"   Created: {agent_dir}")

    # 2. Create database entry
    if not args.skip_db:
        print("2. Creating database entry...")
        if create_database_entry(args.agent_name, args.endowment):
            print(f"   Balance: {args.endowment} tokens")
        else:
            print("   Warning: Database entry failed")
    else:
        print("2. Skipping database entry")

    # 3. Create Forgejo user
    if not args.skip_forgejo:
        print("3. Creating Forgejo user...")
        forgejo_info = create_forgejo_user(args.agent_name)
        if forgejo_info:
            print(f"   User: {forgejo_info['username']}")
            update_config_with_forgejo(agent_dir, forgejo_info)
            print(f"   Token saved to config.json")
        else:
            print("   Warning: Forgejo user creation failed")
    else:
        print("3. Skipping Forgejo user")

    # 4. Create Zulip bot
    if not args.skip_zulip:
        print("4. Creating Zulip bot...")
        api_key = get_zulip_admin_api_key()
        if api_key:
            bot_info = create_zulip_bot(args.agent_name, api_key)
            if bot_info:
                print(f"   Bot: {bot_info['email']}")
                save_zulip_credentials(agent_dir, bot_info)
                print(f"   Saved: {agent_dir}/.zuliprc")

                # Subscribe to channels
                if subscribe_bot_to_channels(bot_info['email'], bot_info['api_key']):
                    print("   Subscribed to: #job-board, #results, #system")
            else:
                print("   Warning: Bot creation failed")
        else:
            print("   Warning: Could not get admin API key")
    else:
        print("4. Skipping Zulip bot")

    print(f"""
✓ Agent {args.agent_name} created!

Directory: {agent_dir}
Files:
  - config.json   (model, endowment, Forgejo token)
  - agent.md      (personality - edit this!)
  - memories/     (persistent notes - organize however you want)
  - skills/       (reusable templates)
  - .zuliprc      (Zulip bot credentials)

Accounts:
  - Database:     {args.endowment} tokens
  - Zulip bot:    {args.agent_name.replace('_', '-')}-bot@localhost
  - Forgejo:      {args.agent_name}@agent.economy

Next steps:
  1. Edit {agent_dir}/agent.md to define personality
  2. Run: python src/runner/runner.py {args.agent_name}
""")


if __name__ == "__main__":
    main()
