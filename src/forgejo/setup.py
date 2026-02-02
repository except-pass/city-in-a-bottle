#!/usr/bin/env python3
"""
Forgejo Setup for Agent Economy.

Creates:
- Admin user (operator)
- Organization (workspace)
- Agent users with limited permissions
- Branch protection rules

Run after starting Forgejo: python src/forgejo/setup.py
"""

import argparse
import json
import sys
import time

import httpx

DEFAULT_URL = "http://localhost:3000"
DEFAULT_ADMIN_USER = "operator"
DEFAULT_ADMIN_PASS = "operator_dev_123"
DEFAULT_ADMIN_EMAIL = "operator@agent.economy"
DEFAULT_ORG = "workspace"


def wait_for_forgejo(base_url: str, timeout: int = 60) -> bool:
    """Wait for Forgejo to be ready."""
    print(f"Waiting for Forgejo at {base_url}...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = httpx.get(f"{base_url}/api/healthz", timeout=5)
            if resp.status_code == 200:
                print("Forgejo is ready!")
                return True
        except Exception:
            pass
        time.sleep(2)
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

    with httpx.Client(timeout=30) as client:
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


def create_admin_user(base_url: str, username: str, password: str, email: str) -> bool:
    """Create the initial admin user via the install API."""
    print(f"Creating admin user: {username}")

    # First check if already installed
    status, _ = api_request("GET", f"{base_url}/api/v1/users/{username}")
    if status == 200:
        print(f"  User {username} already exists")
        return True

    # Try to create via install endpoint (only works on fresh install)
    # Forgejo requires first user to be created via web UI or CLI
    # We'll use the admin create user API if we have a token

    print(f"  Note: Create admin user via Forgejo web UI at {base_url}")
    print(f"  Username: {username}")
    print(f"  Password: {password}")
    print(f"  Email: {email}")
    print(f"  Check 'Administrator' option")
    return False


def get_or_create_token(base_url: str, username: str, password: str) -> str:
    """Get an API token for the user."""
    print(f"Getting API token for {username}...")

    # Try basic auth to create token
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{base_url}/api/v1/users/{username}/tokens",
            auth=(username, password),
            json={"name": "agent-economy-setup", "scopes": ["all"]},
        )
        if resp.status_code == 201:
            token = resp.json().get("sha1")
            print(f"  Created new token")
            return token
        elif resp.status_code == 422:
            # Token with this name exists, try to use existing
            print(f"  Token already exists, please provide it manually")
            return ""
        else:
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
        {"username": org_name, "full_name": "Agent Economy Workspace"},
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
        print(f"  Note: Could not add to org (may need manual setup): {status}")

    # Create token for agent
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{base_url}/api/v1/users/{username}/tokens",
            auth=(username, password),
            json={"name": "agent-token", "scopes": ["write:repository", "read:organization"]},
        )
        if resp.status_code == 201:
            agent_token = resp.json().get("sha1")
            print(f"  Created API token for {username}")
            return {"username": username, "token": agent_token}
        else:
            print(f"  Failed to create token: {resp.status_code}")
            return {"username": username, "token": ""}


def create_repo(
    base_url: str,
    token: str,
    org_name: str,
    repo_name: str,
    description: str = "",
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
            "auto_init": True,
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
) -> bool:
    """Set up branch protection - require PR, no direct push."""
    print(f"Protecting branch: {owner}/{repo}:{branch}")

    # Create branch protection rule
    status, data = api_request(
        "POST",
        f"{base_url}/api/v1/repos/{owner}/{repo}/branch_protections",
        {
            "branch_name": branch,
            "enable_push": False,  # No direct push
            "enable_push_whitelist": True,
            "push_whitelist_usernames": [],  # Nobody can push directly
            "enable_merge_whitelist": True,
            "merge_whitelist_usernames": ["operator"],  # Only operator can merge
            "require_signed_commits": False,
            "protected_file_patterns": "",
            "block_on_rejected_reviews": True,
            "block_on_outdated_branch": False,
            "dismiss_stale_approvals": True,
        },
        token=token,
    )
    if status == 201:
        print(f"  Branch protection enabled")
        return True
    elif status == 422:
        print(f"  Branch protection may already exist")
        return True
    else:
        print(f"  Failed to protect branch: {status} {data}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Set up Forgejo for Agent Economy")
    parser.add_argument("--url", default=DEFAULT_URL, help="Forgejo URL")
    parser.add_argument("--admin-user", default=DEFAULT_ADMIN_USER, help="Admin username")
    parser.add_argument("--admin-pass", default=DEFAULT_ADMIN_PASS, help="Admin password")
    parser.add_argument("--admin-email", default=DEFAULT_ADMIN_EMAIL, help="Admin email")
    parser.add_argument("--org", default=DEFAULT_ORG, help="Organization name")
    parser.add_argument("--token", help="Admin API token (if already created)")
    parser.add_argument("--agents", nargs="+", default=["agent_alpha", "agent_chaos"],
                        help="Agent usernames to create")
    parser.add_argument("--agent-pass", default="agent_dev_123", help="Password for agent users")
    parser.add_argument("--create-repo", help="Create a sample repository")
    args = parser.parse_args()

    # Wait for Forgejo
    if not wait_for_forgejo(args.url):
        print("ERROR: Forgejo not available")
        sys.exit(1)

    # Get or prompt for admin token
    token = args.token
    if not token:
        create_admin_user(args.url, args.admin_user, args.admin_pass, args.admin_email)
        token = get_or_create_token(args.url, args.admin_user, args.admin_pass)

    if not token:
        print("\nPlease create the admin user via web UI, then run:")
        print(f"  python src/forgejo/setup.py --token YOUR_TOKEN")
        sys.exit(1)

    # Create organization
    if not create_organization(args.url, token, args.org):
        print("Failed to create organization")
        sys.exit(1)

    # Create agent users
    agent_tokens = {}
    for agent in args.agents:
        result = create_agent_user(args.url, token, agent, args.agent_pass, args.org)
        if result.get("token"):
            agent_tokens[agent] = result["token"]

    # Create sample repo if requested
    if args.create_repo:
        if create_repo(args.url, token, args.org, args.create_repo, "Agent workspace repository"):
            protect_branch(args.url, token, args.org, args.create_repo)

    # Output summary
    print("\n" + "=" * 50)
    print("SETUP COMPLETE")
    print("=" * 50)
    print(f"\nForgejo URL: {args.url}")
    print(f"Organization: {args.org}")
    print(f"\nAdmin credentials:")
    print(f"  Username: {args.admin_user}")
    print(f"  Password: {args.admin_pass}")

    if agent_tokens:
        print(f"\nAgent tokens (add to agent config):")
        for agent, tok in agent_tokens.items():
            print(f"  {agent}: {tok}")

    print(f"\nTo create a new repo for agents:")
    print(f"  python src/forgejo/setup.py --token TOKEN --create-repo my-repo")


if __name__ == "__main__":
    main()
