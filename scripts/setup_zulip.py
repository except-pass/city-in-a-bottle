#!/usr/bin/env python3
"""
Zulip Setup Script - Fully Automated

Initializes Zulip for the agent economy with zero manual steps.
Safe to run multiple times (idempotent) - will not recreate existing resources.

How it works:
1. Waits for Zulip container to be healthy
2. Creates realm and admin user via Django management commands (if needed)
3. Sets/ensures admin password via Django shell
4. Creates system channels (#job-board, #results, #system) via API
5. Creates bot accounts for each agent in agents/ directory
6. Saves bot credentials (.zuliprc) to agent directories
7. Subscribes bots to system channels

Usage:
    # Full setup (waits for Zulip to be ready)
    python scripts/setup_zulip.py

    # Skip wait (Zulip already running)
    python scripts/setup_zulip.py --skip-wait

    # Custom Zulip URL
    python scripts/setup_zulip.py --zulip-url https://zulip.example.com

Prerequisites:
    - Docker with agent_economy_zulip container running
    - requests library: pip install requests

Configuration:
    Admin credentials are set below. Change for production deployments.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
import urllib3

# Disable SSL warnings for self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
            resp = requests.get(f"{base_url}/health", timeout=5, verify=False)
            if resp.status_code in (200, 403):  # 403 is OK - means server is up
                print("Zulip is ready!")
                return True
        except requests.exceptions.RequestException:
            pass

        print(f"  Attempt {attempt + 1}/{max_attempts} - not ready yet...")
        time.sleep(delay)

    print("ERROR: Zulip did not become ready in time")
    return False


def run_zulip_manage(container: str, *args) -> subprocess.CompletedProcess:
    """Run a Zulip management command in the container as zulip user.

    Uses `docker exec -u zulip` to run commands as the zulip user,
    which is required for accessing Zulip secrets and databases.
    """
    cmd = [
        "docker", "exec", "-u", "zulip", container,
        "/home/zulip/deployments/current/manage.py"
    ] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def ensure_admin_password(container: str) -> bool:
    """Ensure admin user has the correct password set.

    This is needed because Zulip may auto-create the admin user
    from SETTING_ZULIP_ADMINISTRATOR but without a password.
    """
    result = run_zulip_manage(
        container, "shell", "-c",
        f"""
from zerver.models import UserProfile
user = UserProfile.objects.get(delivery_email='{ADMIN_EMAIL}')
user.set_password('{ADMIN_PASSWORD}')
user.is_realm_admin = True
user.save()
print('OK')
"""
    )
    return "OK" in result.stdout


def create_admin_user(container: str) -> bool:
    """Create the admin user for the organization.

    Handles several scenarios:
    1. Fresh Zulip: Creates realm + admin user
    2. Auto-created realm (from env vars): Creates admin user in existing realm
    3. Existing admin: Just ensures password is set correctly
    """
    print("Creating admin user...")

    # Check if admin exists (in any realm)
    result = run_zulip_manage(
        container, "shell", "-c",
        f"from zerver.models import UserProfile; print(UserProfile.objects.filter(delivery_email='{ADMIN_EMAIL}').exists())"
    )

    if "True" in result.stdout:
        print("  Admin user already exists, ensuring password is set...")
        if ensure_admin_password(container):
            print("  Admin user ready")
            return True
        else:
            print("  Warning: Could not set admin password")
            return True  # Continue anyway, user might have set a custom password

    # Check if main realm exists (empty string_id = root domain)
    result = run_zulip_manage(
        container, "shell", "-c",
        "from zerver.models import Realm; print(Realm.objects.filter(string_id='').exists())"
    )

    if "True" in result.stdout:
        print("  Realm already exists, creating admin user...")
        # Create admin user in existing realm
        result = run_zulip_manage(
            container, "create_user",
            "--realm", "",  # Empty string = root domain
            "--email", ADMIN_EMAIL,
            "--full-name", "Admin",
            "--password", ADMIN_PASSWORD,
            "--is-admin"
        )
    else:
        print("  Creating realm and admin user...")
        # Create realm with admin user in one command
        result = run_zulip_manage(
            container, "create_realm",
            "--string-id", "",  # Root domain
            "--password", ADMIN_PASSWORD,
            "Agent Economy",
            ADMIN_EMAIL,
            "Admin"
        )

    if result.returncode != 0 and "already exists" not in result.stderr.lower():
        print(f"  Error: {result.stderr}")
        return False

    print("  Admin user ready")
    return True


def get_admin_api_key(container: str) -> str | None:
    """Get API key for admin user."""
    result = run_zulip_manage(
        container, "shell", "-c",
        f"from zerver.models import UserProfile; u = UserProfile.objects.get(delivery_email='{ADMIN_EMAIL}'); print(u.api_key)"
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
            },
            verify=False
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
        },
        verify=False
    )

    if resp.status_code == 200:
        result = resp.json()
        if result.get("result") == "success":
            return {
                "user_id": result["user_id"],
                "api_key": result["api_key"],
                "email": f"{bot_name}-bot@agent-economy.zulip.localhost",
            }
    elif resp.status_code == 400 and ("already exists" in resp.text.lower() or "already in use" in resp.text.lower()):
        # Bot exists, get its info from the bots list
        resp = requests.get(
            f"{base_url}/api/v1/bots",
            auth=(admin_email, api_key),
            verify=False
        )
        if resp.status_code == 200:
            for bot in resp.json().get("bots", []):
                # Bot email can be in "email" or "username" field depending on API version
                bot_email = bot.get("email") or bot.get("username", "")
                # Match by bot name in the email/username (e.g., "agent-alpha-bot@...")
                if f"{bot_name}-bot" in bot_email:
                    return {
                        "user_id": bot.get("user_id"),
                        "api_key": bot["api_key"],
                        "email": bot_email,
                    }

    print(f"    Error creating bot: {resp.status_code} - {resp.text}")
    return None


def subscribe_bot_to_channels(base_url: str, bot_email: str, api_key: str) -> bool:
    """Subscribe a bot to all system channels."""
    subscriptions = [{"name": ch["name"]} for ch in SYSTEM_CHANNELS]

    resp = requests.post(
        f"{base_url}/api/v1/users/me/subscriptions",
        auth=(bot_email, api_key),
        data={"subscriptions": json.dumps(subscriptions)},
        verify=False
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
        default=os.environ.get("ZULIP_URL", "https://localhost:8443"),
        help="Zulip server URL (default: https://localhost:8443)"
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
