#!/usr/bin/env python3
"""
Forgejo Setup for City in a Bottle - Fully Automated.

Creates:
- Initial install (via Playwright if needed)
- Admin user (operator)
- Organization (workspace)
- Agent users with limited permissions
- Branch protection rules

Safe to run multiple times (idempotent).

Usage:
    source .venv/bin/activate
    python src/forgejo/setup.py
    python src/forgejo/setup.py --agents test_agent agent_alpha
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

# Disable SSL verification for Caddy's local CA certs
SSL_VERIFY = False

# URL for setup (from host)
DEFAULT_URL = "http://code.localhost"
# URL for agents (from inside container)
AGENT_FORGEJO_URL = "http://forgejo:3000"
DEFAULT_ADMIN_USER = "operator"
DEFAULT_ADMIN_PASS = "operator_dev_123"
DEFAULT_ADMIN_EMAIL = "operator@agent.economy"
DEFAULT_ORG = "workspace"
DEFAULT_AGENT_PASS = "agent_dev_123"

# Default repositories to create (per Constitution Article 2)
DEFAULT_REPOS = [
    {"name": "agent-contributions", "description": "Shared agent work - PRs welcome"},
    {"name": "agent-economy", "description": "Infrastructure repo - agents can submit PRs to improve the system"},
]

# Agents directory
AGENTS_DIR = Path(__file__).parent.parent.parent / ".data" / "agents"


def wait_for_forgejo(base_url: str, timeout: int = 60) -> bool:
    """Wait for Forgejo to be ready."""
    print(f"Waiting for Forgejo at {base_url}...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = httpx.get(f"{base_url}/api/healthz", timeout=5, verify=SSL_VERIFY)
            if resp.status_code == 200:
                print("Forgejo is ready!")
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def is_installed(base_url: str) -> bool:
    """Check if Forgejo has completed initial installation."""
    try:
        resp = httpx.get(f"{base_url}/api/v1/version", timeout=5, verify=SSL_VERIFY)
        return resp.status_code == 200
    except Exception:
        return False


def run_install_wizard(base_url: str, admin_user: str, admin_pass: str, admin_email: str) -> bool:
    """Run the Forgejo install wizard using Playwright."""
    print("Running initial install wizard via Playwright...")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: Playwright not installed. Run: pip install playwright && playwright install chromium")
        return False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            # Go to install page
            page.goto(f"{base_url}/install", timeout=30000)
            page.wait_for_load_state("networkidle")

            # Check if we're on install page or redirected (already installed)
            if "/install" not in page.url:
                print("  Already installed (redirected from install page)")
                browser.close()
                return True

            # Fill in the install form
            # Database settings - SQLite is default, leave it

            # General settings - Instance title (if visible)
            instance_title = page.locator('input[id="app_name"]')
            if instance_title.count() > 0 and instance_title.is_visible():
                instance_title.fill("City in a Bottle")

            # Expand "Administrator account settings" section
            # Use JavaScript to expand all collapsed sections and make admin fields visible
            page.evaluate("""
                // Expand all details elements
                document.querySelectorAll('details').forEach(d => d.setAttribute('open', 'open'));
                // Also try to make admin section visible by removing hidden classes
                const adminSection = document.querySelector('#admin_name')?.closest('.field, .fields, details, .hidden');
                if (adminSection) {
                    adminSection.style.display = 'block';
                    adminSection.classList.remove('hidden');
                }
                // Make the admin input itself visible
                const adminInput = document.querySelector('#admin_name');
                if (adminInput) {
                    adminInput.style.display = 'block';
                    adminInput.style.visibility = 'visible';
                    // Also show parent elements
                    let parent = adminInput.parentElement;
                    while (parent && parent !== document.body) {
                        parent.style.display = parent.style.display === 'none' ? 'block' : parent.style.display;
                        parent.style.visibility = 'visible';
                        if (parent.tagName === 'DETAILS') parent.setAttribute('open', 'open');
                        parent = parent.parentElement;
                    }
                }
            """)
            page.wait_for_timeout(1000)

            # Take debug screenshot
            page.screenshot(path="/tmp/forgejo_debug_expanded.png", full_page=True)

            # Now fill admin fields using JavaScript as fallback if needed
            admin_name = page.locator('input[id="admin_name"]')
            if not admin_name.is_visible():
                # Force fill via JavaScript
                page.evaluate(f"""
                    const input = document.querySelector('#admin_name');
                    if (input) {{
                        input.value = '{admin_user}';
                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}
                """)
            else:
                admin_name.fill(admin_user)

            # Fill password fields
            admin_passwd = page.locator('input[id="admin_passwd"]')
            if admin_passwd.is_visible():
                admin_passwd.fill(admin_pass)
            else:
                page.evaluate(f"""
                    const input = document.querySelector('#admin_passwd');
                    if (input) {{ input.value = '{admin_pass}'; input.dispatchEvent(new Event('input', {{ bubbles: true }})); }}
                """)

            admin_confirm = page.locator('input[id="admin_confirm_passwd"]')
            if admin_confirm.is_visible():
                admin_confirm.fill(admin_pass)
            else:
                page.evaluate(f"""
                    const input = document.querySelector('#admin_confirm_passwd');
                    if (input) {{ input.value = '{admin_pass}'; input.dispatchEvent(new Event('input', {{ bubbles: true }})); }}
                """)

            admin_email_field = page.locator('input[id="admin_email"]')
            if admin_email_field.is_visible():
                admin_email_field.fill(admin_email)
            else:
                page.evaluate(f"""
                    const input = document.querySelector('#admin_email');
                    if (input) {{ input.value = '{admin_email}'; input.dispatchEvent(new Event('input', {{ bubbles: true }})); }}
                """)

            # Find and click "Install Forgejo" button
            submit_button = page.locator('button:has-text("Install Forgejo")')
            if submit_button.count() == 0:
                submit_button = page.locator('button[type="submit"]')

            # Scroll to button and click
            submit_button.scroll_into_view_if_needed()
            submit_button.click()

            # Wait for redirect (install complete) - can take a while
            page.wait_for_url(lambda url: "/install" not in url, timeout=120000)

            print("  Install wizard completed successfully!")
            browser.close()
            return True

        except Exception as e:
            print(f"  Install wizard error: {e}")
            # Take screenshot for debugging
            screenshot_path = "/tmp/forgejo_install_error.png"
            page.screenshot(path=screenshot_path, full_page=True)
            print(f"  Screenshot saved to {screenshot_path}")
            browser.close()
            return False


def api_request(
    method: str,
    url: str,
    data: dict = None,
    token: str = None,
) -> tuple[int, dict]:
    """Make API request, return (status_code, response_json)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"token {token}"

    with httpx.Client(timeout=30, verify=SSL_VERIFY) as client:
        if method == "GET":
            resp = client.get(url, headers=headers)
        elif method == "POST":
            resp = client.post(url, headers=headers, json=data)
        elif method == "PUT":
            resp = client.put(url, headers=headers, json=data)
        elif method == "PATCH":
            resp = client.patch(url, headers=headers, json=data)
        elif method == "DELETE":
            resp = client.delete(url, headers=headers)
        else:
            raise ValueError(f"Unknown method: {method}")

        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, {"text": resp.text}


def get_or_create_token(base_url: str, username: str, password: str) -> str:
    """Get an API token for the user."""
    print(f"Getting API token for {username}...")

    with httpx.Client(timeout=30, verify=SSL_VERIFY) as client:
        # First try to create a new token
        resp = client.post(
            f"{base_url}/api/v1/users/{username}/tokens",
            auth=(username, password),
            json={"name": "agent-economy-setup", "scopes": ["all"]},
        )
        if resp.status_code == 201:
            token = resp.json().get("sha1")
            print(f"  Created new token")
            return token
        elif resp.status_code in (400, 422) and "already" in resp.text.lower():
            # Token exists, delete and recreate
            print(f"  Token exists, recreating...")
            # List tokens
            resp = client.get(
                f"{base_url}/api/v1/users/{username}/tokens",
                auth=(username, password),
            )
            if resp.status_code == 200:
                for tok in resp.json():
                    if tok.get("name") == "agent-economy-setup":
                        # Delete it
                        client.delete(
                            f"{base_url}/api/v1/users/{username}/tokens/{tok['id']}",
                            auth=(username, password),
                        )
                        break
            # Create new
            resp = client.post(
                f"{base_url}/api/v1/users/{username}/tokens",
                auth=(username, password),
                json={"name": "agent-economy-setup", "scopes": ["all"]},
            )
            if resp.status_code == 201:
                token = resp.json().get("sha1")
                print(f"  Created new token")
                return token

        print(f"  Failed to create token: {resp.status_code} {resp.text}")
        return ""


def create_organization(base_url: str, token: str, org_name: str) -> bool:
    """Create the workspace organization."""
    print(f"Creating organization: {org_name}")

    # Check if exists
    status, data = api_request("GET", f"{base_url}/api/v1/orgs/{org_name}", token=token)
    if status == 200:
        print(f"  Organization {org_name} already exists")
        return True

    # Create org
    status, data = api_request(
        "POST",
        f"{base_url}/api/v1/orgs",
        {"username": org_name, "full_name": "City in a Bottle Workspace"},
        token=token,
    )
    if status == 201:
        print(f"  Created organization {org_name}")
        return True
    else:
        print(f"  Failed to create org: {status} {data}")
        return False


def create_agent_user(
    base_url: str,
    token: str,
    username: str,
    password: str,
    org_name: str,
) -> dict:
    """Create an agent user and add to organization."""
    print(f"Creating agent user: {username}")

    # Check if exists
    status, data = api_request("GET", f"{base_url}/api/v1/users/{username}", token=token)
    if status == 200:
        print(f"  User {username} already exists")
    else:
        # Create user (requires admin)
        status, data = api_request(
            "POST",
            f"{base_url}/api/v1/admin/users",
            {
                "username": username,
                "password": password,
                "email": f"{username}@agent.economy",
                "must_change_password": False,
            },
            token=token,
        )
        if status == 201:
            print(f"  Created user {username}")
        else:
            print(f"  Failed to create user: {status} {data}")
            return {}

    # Add to organization as member (not owner)
    status, data = api_request(
        "PUT",
        f"{base_url}/api/v1/orgs/{org_name}/members/{username}",
        token=token,
    )
    if status == 204:
        print(f"  Added {username} to {org_name}")
    else:
        print(f"  Note: Could not add to org: {status}")

    # Create token for agent
    with httpx.Client(timeout=30, verify=SSL_VERIFY) as client:
        # Delete existing token if any
        resp = client.get(
            f"{base_url}/api/v1/users/{username}/tokens",
            auth=(username, password),
        )
        if resp.status_code == 200:
            for tok in resp.json():
                if tok.get("name") == "agent-token":
                    client.delete(
                        f"{base_url}/api/v1/users/{username}/tokens/{tok['id']}",
                        auth=(username, password),
                    )
                    break

        # Create new token
        resp = client.post(
            f"{base_url}/api/v1/users/{username}/tokens",
            auth=(username, password),
            json={"name": "agent-token", "scopes": ["read:user", "write:repository", "read:repository", "write:issue", "read:organization"]},
        )
        if resp.status_code == 201:
            agent_token = resp.json().get("sha1")
            print(f"  Created API token for {username}")
            return {"username": username, "token": agent_token}
        else:
            print(f"  Failed to create token: {resp.status_code}")
            return {"username": username, "token": ""}


def save_agent_forgejo_config(agent_name: str, forgejo_url: str, token: str) -> bool:
    """Save Forgejo credentials to agent's config.json."""
    agent_dir = AGENTS_DIR / agent_name
    config_path = agent_dir / "config.json"

    if not config_path.exists():
        print(f"  Warning: Agent config not found at {config_path}")
        return False

    config = json.loads(config_path.read_text())
    config["forgejo"] = {
        "url": forgejo_url,
        "username": agent_name,
        "token": token,
    }
    config_path.write_text(json.dumps(config, indent=4) + "\n")
    print(f"  Saved Forgejo config to {config_path}")
    return True


def create_repo(
    base_url: str,
    token: str,
    org_name: str,
    repo_name: str,
    description: str = "",
    auto_init: bool = True,
) -> bool:
    """Create a repository in the organization."""
    print(f"Creating repository: {org_name}/{repo_name}")

    # Check if exists
    status, data = api_request(
        "GET",
        f"{base_url}/api/v1/repos/{org_name}/{repo_name}",
        token=token,
    )
    if status == 200:
        print(f"  Repository already exists")
        return True

    # Create repo
    status, data = api_request(
        "POST",
        f"{base_url}/api/v1/orgs/{org_name}/repos",
        {
            "name": repo_name,
            "description": description,
            "private": False,
            "auto_init": auto_init,
            "default_branch": "main",
        },
        token=token,
    )
    if status == 201:
        print(f"  Created repository {repo_name}")
        return True
    else:
        print(f"  Failed to create repo: {status} {data}")
        return False


def protect_branch(
    base_url: str,
    token: str,
    owner: str,
    repo: str,
    branch: str = "main",
    required_approvals: int = 2,
    admin_user: str = DEFAULT_ADMIN_USER,
) -> bool:
    """Set up branch protection - require PR with approvals to merge.

    Per Constitution/Laws: PRs need required_approvals to merge.
    This is the democratic voting mechanism - approvals = votes.
    The admin (operator) is whitelisted to push directly for infra updates.
    """
    print(f"Protecting branch: {owner}/{repo}:{branch} (requires {required_approvals} approvals)")

    protection_config = {
        "branch_name": branch,
        "enable_push": True,  # Push allowed, but only for whitelisted users
        "enable_push_whitelist": True,
        "push_whitelist_usernames": [admin_user],  # Operator can push directly
        "enable_merge_whitelist": False,  # Anyone can merge if approvals met
        "require_signed_commits": False,
        "protected_file_patterns": "",
        "block_on_rejected_reviews": True,
        "block_on_outdated_branch": False,
        "dismiss_stale_approvals": True,
        "required_approvals": required_approvals,  # Democratic voting threshold
        "enable_approvals_whitelist": False,  # Any agent can approve
    }

    # Try to create; if it already exists, update it instead
    status, data = api_request(
        "POST",
        f"{base_url}/api/v1/repos/{owner}/{repo}/branch_protections",
        protection_config,
        token=token,
    )
    if status == 201:
        print(f"  Branch protection enabled (push whitelist: [{admin_user}])")
        return True
    elif status in (422, 403):
        # Already exists - update it
        status, data = api_request(
            "PATCH",
            f"{base_url}/api/v1/repos/{owner}/{repo}/branch_protections/{branch}",
            protection_config,
            token=token,
        )
        if status == 200:
            print(f"  Branch protection updated (push whitelist: [{admin_user}])")
            return True
        print(f"  Failed to update branch protection: {status} {data}")
        return False
    else:
        print(f"  Failed to protect branch: {status} {data}")
        return False


def discover_agents() -> list[str]:
    """Find all agent directories."""
    agents = []
    if not AGENTS_DIR.exists():
        return agents
    for item in AGENTS_DIR.iterdir():
        if item.is_dir() and (item / "config.json").exists():
            agents.append(item.name)
    return sorted(agents)


def push_repo_to_forgejo(
    base_url: str,
    token: str,
    org: str,
    repo_name: str = "agent-economy",
) -> bool:
    """Push the local repo to Forgejo so agents can submit PRs.

    Sets up Forgejo as a git remote and pushes current branch.
    Idempotent - safe to run multiple times.
    """
    import subprocess

    repo_dir = Path(__file__).parent.parent.parent  # agent_economy root
    remote_name = "forgejo"
    remote_url = f"{base_url}/{org}/{repo_name}.git"

    print(f"Pushing repo to Forgejo: {org}/{repo_name}")

    # Check if remote already exists
    result = subprocess.run(
        ["git", "remote", "get-url", remote_name],
        capture_output=True, text=True, cwd=repo_dir
    )

    if result.returncode == 0:
        # Remote exists, check if URL matches
        current_url = result.stdout.strip()
        if current_url != remote_url:
            print(f"  Updating remote URL...")
            subprocess.run(
                ["git", "remote", "set-url", remote_name, remote_url],
                cwd=repo_dir
            )
    else:
        # Add remote
        print(f"  Adding remote '{remote_name}'...")
        subprocess.run(
            ["git", "remote", "add", remote_name, remote_url],
            cwd=repo_dir
        )

    # Get current branch
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, cwd=repo_dir
    )
    current_branch = result.stdout.strip()

    # Push to Forgejo (force to handle initial empty repo with different history)
    print(f"  Pushing {current_branch} to {remote_name}...")

    # Use token auth in URL for push
    auth_url = f"http://operator:{token}@code.localhost/{org}/{repo_name}.git"

    result = subprocess.run(
        ["git", "push", "--force", auth_url, f"{current_branch}:main"],
        capture_output=True, text=True, cwd=repo_dir
    )

    if result.returncode == 0:
        print(f"  Successfully pushed to Forgejo")
        return True
    else:
        print(f"  Push failed: {result.stderr}")
        # Try without force
        result = subprocess.run(
            ["git", "push", auth_url, f"{current_branch}:main"],
            capture_output=True, text=True, cwd=repo_dir
        )
        if result.returncode == 0:
            print(f"  Successfully pushed to Forgejo")
            return True
        print(f"  Push still failed: {result.stderr}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Set up Forgejo for City in a Bottle")
    parser.add_argument("--url", default=DEFAULT_URL, help="Forgejo URL")
    parser.add_argument("--admin-user", default=DEFAULT_ADMIN_USER, help="Admin username")
    parser.add_argument("--admin-pass", default=DEFAULT_ADMIN_PASS, help="Admin password")
    parser.add_argument("--admin-email", default=DEFAULT_ADMIN_EMAIL, help="Admin email")
    parser.add_argument("--org", default=DEFAULT_ORG, help="Organization name")
    parser.add_argument("--token", help="Admin API token (skip install if provided)")
    parser.add_argument("--agents", nargs="*", help="Agent usernames to create (default: discover from .data/agents/)")
    parser.add_argument("--agent-pass", default=DEFAULT_AGENT_PASS, help="Password for agent users")
    parser.add_argument("--skip-install", action="store_true", help="Skip install wizard check")
    args = parser.parse_args()

    # Wait for Forgejo
    if not wait_for_forgejo(args.url):
        print("ERROR: Forgejo not available")
        sys.exit(1)

    # Check if installed, run wizard if not
    if not args.skip_install and not args.token:
        if not is_installed(args.url):
            if not run_install_wizard(args.url, args.admin_user, args.admin_pass, args.admin_email):
                print("ERROR: Failed to complete install wizard")
                sys.exit(1)
            # Wait a moment for Forgejo to restart after install
            time.sleep(3)
            wait_for_forgejo(args.url)

    # Get admin token
    token = args.token
    if not token:
        token = get_or_create_token(args.url, args.admin_user, args.admin_pass)

    if not token:
        print("ERROR: Could not get admin token")
        sys.exit(1)

    # Create organization
    if not create_organization(args.url, token, args.org):
        print("Failed to create organization")
        sys.exit(1)

    # Discover or use provided agents
    agents = args.agents if args.agents is not None else discover_agents()
    if not agents:
        print("No agents specified or found in .data/agents/")

    # Create agent users
    agent_tokens = {}
    for agent in agents:
        result = create_agent_user(args.url, token, agent, args.agent_pass, args.org)
        if result.get("token"):
            agent_tokens[agent] = result["token"]
            # Save to agent config (use container URL, not host URL)
            save_agent_forgejo_config(agent, AGENT_FORGEJO_URL, result["token"])

    # Create default repos (per Constitution)
    for repo_config in DEFAULT_REPOS:
        repo_name = repo_config["name"]
        # agent-economy is special: we push our code to it, so no auto_init
        is_infra_repo = repo_name == "agent-economy"
        if create_repo(args.url, token, args.org, repo_name, repo_config["description"], auto_init=not is_infra_repo):
            if is_infra_repo:
                # Push code BEFORE protecting (protection blocks force push)
                push_repo_to_forgejo(args.url, token, args.org, repo_name)
            protect_branch(args.url, token, args.org, repo_name)

    # Output summary
    print("\n" + "=" * 50)
    print("FORGEJO SETUP COMPLETE")
    print("=" * 50)
    print(f"\nForgejo URL: {args.url}")
    print(f"Organization: {args.org}")
    print(f"\nAdmin credentials:")
    print(f"  Username: {args.admin_user}")
    print(f"  Password: {args.admin_pass}")

    if agent_tokens:
        print(f"\nAgent accounts created:")
        for agent in agent_tokens:
            print(f"  {agent} (token saved to config)")

    print(f"\nRepositories:")
    for repo in DEFAULT_REPOS:
        print(f"  {args.org}/{repo['name']}")


if __name__ == "__main__":
    main()
