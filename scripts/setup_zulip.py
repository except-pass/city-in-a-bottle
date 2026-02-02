#!/usr/bin/env python3
"""
Zulip Setup Script

Initializes Zulip for the agent economy:
1. Waits for Zulip to be ready
2. Creates admin user and organization
3. Creates system channels (#job-board, #results, #system)
4. Creates bot accounts for each agent
5. Saves bot credentials to agent directories
6. Subscribes bots to system channels

Usage:
    python scripts/setup_zulip.py [--zulip-url URL] [--agents-dir DIR]
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

# System channels that all bots should be subscribed to
SYSTEM_CHANNELS = [
    {"name": "job-board", "description": "Job postings and bids"},
    {"name": "results", "description": "Completed work submissions"},
    {"name": "system", "description": "System announcements and meta"},
]

# Admin credentials for setup
ADMIN_EMAIL = "admin@agent-economy.local"
ADMIN_PASSWORD = "admin-dev-password-123"


def wait_for_zulip(base_url: str, max_attempts: int = 60, delay: int = 5) -> bool:
    """Wait for Zulip server to be ready."""
    print(f"Waiting for Zulip at {base_url}...")

    for attempt in range(max_attempts):
        try:
            resp = requests.get(f"{base_url}/health", timeout=5)
            if resp.status_code == 200:
                print("Zulip is ready!")
                return True
        except requests.exceptions.RequestException:
            pass

        print(f"  Attempt {attempt + 1}/{max_attempts} - not ready yet...")
        time.sleep(delay)

    print("ERROR: Zulip did not become ready in time")
    return False


def run_zulip_manage(container: str, *args) -> subprocess.CompletedProcess:
    """Run a Zulip management command in the container."""
    cmd = ["docker", "exec", container, "/home/zulip/deployments/current/manage.py"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def create_admin_user(container: str) -> bool:
    """Create the admin user for the organization."""
    print("Creating admin user...")

    # Check if admin exists
    result = run_zulip_manage(
        container, "shell", "-c",
        f"from zerver.models import UserProfile; print(UserProfile.objects.filter(delivery_email='{ADMIN_EMAIL}').exists())"
    )

    if "True" in result.stdout:
        print("  Admin user already exists")
        return True

    # Create the realm first if needed
    result = run_zulip_manage(
        container, "shell", "-c",
        "from zerver.models import Realm; print(Realm.objects.filter(string_id='agent-economy').exists())"
    )

    if "True" not in result.stdout:
        print("  Creating realm 'agent-economy'...")
        result = run_zulip_manage(
            container, "create_realm",
            "--realm-subdomain", "agent-economy",
            "--realm-name", "Agent Economy"
        )
        if result.returncode != 0:
            print(f"  Warning: create_realm output: {result.stderr}")

    # Create admin user
    result = run_zulip_manage(
        container, "create_user",
        "--realm", "agent-economy",
        "--email", ADMIN_EMAIL,
        "--full-name", "Admin",
        "--password", ADMIN_PASSWORD,
        "--is-admin"
    )

    if result.returncode != 0 and "already exists" not in result.stderr.lower():
        print(f"  Error creating admin: {result.stderr}")
        return False

    print("  Admin user created")
    return True


def get_admin_api_key(container: str) -> str | None:
    """Get or create API key for admin user."""
    result = run_zulip_manage(
        container, "shell", "-c",
        f"""
from zerver.models import UserProfile
from zerver.lib.api_keys import get_api_key
user = UserProfile.objects.get(delivery_email='{ADMIN_EMAIL}')
print(get_api_key(user))
"""
    )

    if result.returncode == 0:
        api_key = result.stdout.strip().split('\n')[-1]
        if len(api_key) == 32:  # Zulip API keys are 32 chars
            return api_key

    print(f"  Error getting admin API key: {result.stderr}")
    return None


def create_channels(base_url: str, admin_email: str, api_key: str) -> bool:
    """Create system channels."""
    print("Creating system channels...")

    for channel in SYSTEM_CHANNELS:
        print(f"  Creating #{channel['name']}...")

        resp = requests.post(
            f"{base_url}/api/v1/users/me/subscriptions",
            auth=(admin_email, api_key),
            data={
                "subscriptions": json.dumps([{
                    "name": channel["name"],
                    "description": channel["description"],
                }])
            }
        )

        if resp.status_code == 200:
            result = resp.json()
            if result.get("result") == "success":
                print(f"    Created/subscribed to #{channel['name']}")
            else:
                print(f"    Warning: {result}")
        else:
            print(f"    Error: {resp.status_code} - {resp.text}")

    return True


def discover_agents(agents_dir: Path) -> list[str]:
    """Find all agent directories."""
    agents = []
    for item in agents_dir.iterdir():
        if item.is_dir() and (item / "config.json").exists():
            agents.append(item.name)
    return sorted(agents)


def create_bot(base_url: str, admin_email: str, api_key: str, agent_name: str) -> dict | None:
    """Create a bot for an agent."""
    bot_name = agent_name.replace("_", "-")
    full_name = agent_name.replace("_", " ").title()

    # Create bot
    resp = requests.post(
        f"{base_url}/api/v1/bots",
        auth=(admin_email, api_key),
        data={
            "short_name": bot_name,
            "full_name": full_name,
            "bot_type": 1,  # Generic bot
        }
    )

    if resp.status_code == 200:
        result = resp.json()
        if result.get("result") == "success":
            return {
                "user_id": result["user_id"],
                "api_key": result["api_key"],
                "email": f"{bot_name}-bot@agent-economy.zulip.localhost",
            }
    elif resp.status_code == 400 and "already exists" in resp.text.lower():
        # Bot exists, get its info
        resp = requests.get(
            f"{base_url}/api/v1/bots",
            auth=(admin_email, api_key),
        )
        if resp.status_code == 200:
            for bot in resp.json().get("bots", []):
                if bot.get("short_name") == bot_name or bot_name in bot.get("email", ""):
                    return {
                        "user_id": bot["user_id"],
                        "api_key": bot["api_key"],
                        "email": bot["email"],
                    }

    print(f"    Error creating bot: {resp.status_code} - {resp.text}")
    return None


def subscribe_bot_to_channels(base_url: str, bot_email: str, api_key: str) -> bool:
    """Subscribe a bot to all system channels."""
    subscriptions = [{"name": ch["name"]} for ch in SYSTEM_CHANNELS]

    resp = requests.post(
        f"{base_url}/api/v1/users/me/subscriptions",
        auth=(bot_email, api_key),
        data={"subscriptions": json.dumps(subscriptions)}
    )

    return resp.status_code == 200 and resp.json().get("result") == "success"


def save_bot_credentials(agents_dir: Path, agent_name: str, base_url: str, bot_info: dict) -> bool:
    """Save bot credentials to agent directory as .zuliprc file."""
    agent_dir = agents_dir / agent_name
    zuliprc_path = agent_dir / ".zuliprc"

    # Extract site URL (without /api/v1)
    site = base_url.rstrip("/")

    content = f"""[api]
email={bot_info['email']}
key={bot_info['api_key']}
site={site}
"""

    zuliprc_path.write_text(content)
    print(f"    Saved credentials to {zuliprc_path}")
    return True


def setup_agents(base_url: str, admin_email: str, api_key: str, agents_dir: Path) -> bool:
    """Create bots for all agents and save their credentials."""
    print("Setting up agent bots...")

    agents = discover_agents(agents_dir)
    if not agents:
        print("  No agents found in agents directory")
        return True

    print(f"  Found {len(agents)} agents: {', '.join(agents)}")

    for agent_name in agents:
        print(f"  Setting up {agent_name}...")

        # Create bot
        bot_info = create_bot(base_url, admin_email, api_key, agent_name)
        if not bot_info:
            print(f"    Failed to create bot for {agent_name}")
            continue

        print(f"    Bot created: {bot_info['email']}")

        # Subscribe to channels
        if subscribe_bot_to_channels(base_url, bot_info["email"], bot_info["api_key"]):
            print(f"    Subscribed to system channels")
        else:
            print(f"    Warning: Failed to subscribe to channels")

        # Save credentials
        save_bot_credentials(agents_dir, agent_name, base_url, bot_info)

    return True


def main():
    parser = argparse.ArgumentParser(description="Set up Zulip for agent economy")
    parser.add_argument(
        "--zulip-url",
        default=os.environ.get("ZULIP_URL", "http://localhost:8080"),
        help="Zulip server URL (default: http://localhost:8080)"
    )
    parser.add_argument(
        "--agents-dir",
        type=Path,
        default=Path(__file__).parent.parent / "agents",
        help="Path to agents directory"
    )
    parser.add_argument(
        "--container",
        default="agent_economy_zulip",
        help="Zulip Docker container name"
    )
    parser.add_argument(
        "--skip-wait",
        action="store_true",
        help="Skip waiting for Zulip to be ready"
    )

    args = parser.parse_args()

    # Wait for Zulip
    if not args.skip_wait:
        if not wait_for_zulip(args.zulip_url):
            sys.exit(1)

    # Create admin user
    if not create_admin_user(args.container):
        print("Failed to create admin user")
        sys.exit(1)

    # Get admin API key
    api_key = get_admin_api_key(args.container)
    if not api_key:
        print("Failed to get admin API key")
        sys.exit(1)

    print(f"Got admin API key: {api_key[:8]}...")

    # Create channels
    if not create_channels(args.zulip_url, ADMIN_EMAIL, api_key):
        print("Failed to create channels")
        sys.exit(1)

    # Set up agent bots
    if not setup_agents(args.zulip_url, ADMIN_EMAIL, api_key, args.agents_dir):
        print("Failed to set up agents")
        sys.exit(1)

    print("\n✓ Zulip setup complete!")
    print(f"  Web UI: {args.zulip_url}")
    print(f"  Admin login: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")


if __name__ == "__main__":
    main()
