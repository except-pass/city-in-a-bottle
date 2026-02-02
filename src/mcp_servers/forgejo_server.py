#!/usr/bin/env python3
"""
MCP Server for Forgejo Git Operations.

Permission Model:
- Agents have FULL control over repos they create/own
- On operator/other-owned repos: can read, branch, PR, comment - but NOT push to main
- Organization repos (workspace): follow branch protection rules

This allows agents to collaborate via PRs while maintaining sovereignty over their own repos.
"""

import base64
import json
import os
from typing import Any, Optional

import httpx
from mcp.server import FastMCP

# Configuration from environment
FORGEJO_URL = os.environ.get("FORGEJO_URL", "http://localhost:3000")
FORGEJO_TOKEN = os.environ.get("FORGEJO_TOKEN", "")
AGENT_ID = os.environ.get("AGENT_ID", "unknown_agent")

# Protected users - agents cannot push to main on repos owned by these users
PROTECTED_OWNERS = {"operator"}

# Create MCP server
mcp = FastMCP(
    name="agent-economy-forgejo",
    instructions="""Git tools for code collaboration.

You can:
- Create and fully manage your own repositories
- Read any public repo, create branches, open PRs
- Comment on and review pull requests

On repos you don't own:
- You must use branches + PRs (no direct push to main)

Use create_repo() to create repos you'll have full control over.""",
)


def api_request(
    method: str,
    endpoint: str,
    data: Optional[dict] = None,
    token: Optional[str] = None,
) -> dict:
    """Make an API request to Forgejo."""
    url = f"{FORGEJO_URL}/api/v1{endpoint}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token or FORGEJO_TOKEN:
        headers["Authorization"] = f"token {token or FORGEJO_TOKEN}"

    with httpx.Client(timeout=30.0) as client:
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

        if resp.status_code >= 400:
            return {"error": f"API error {resp.status_code}: {resp.text}"}

        if resp.status_code == 204:
            return {"success": True}

        try:
            return resp.json()
        except Exception:
            return {"success": True, "text": resp.text}


def get_repo_owner(owner: str, repo: str) -> Optional[str]:
    """Get the actual owner of a repository."""
    result = api_request("GET", f"/repos/{owner}/{repo}")
    if isinstance(result, dict) and "owner" in result:
        return result["owner"].get("login")
    return None


def agent_owns_repo(owner: str, repo: str) -> bool:
    """Check if the current agent owns this repository."""
    result = api_request("GET", f"/repos/{owner}/{repo}")
    if isinstance(result, dict) and "owner" in result:
        repo_owner = result["owner"].get("login")
        return repo_owner == AGENT_ID
    return False


def is_protected_branch(owner: str, repo: str, branch: str) -> bool:
    """Check if a branch is protected and agent doesn't own the repo."""
    if branch.lower() not in ("main", "master"):
        return False
    # If agent owns the repo, they can push anywhere
    if agent_owns_repo(owner, repo):
        return False
    return True


# =============================================================================
# REPOSITORY MANAGEMENT
# =============================================================================

@mcp.tool()
def list_repos(owner: str = "") -> str:
    """
    List repositories. Shows org repos or user repos.

    Args:
        owner: Organization or username. Empty = list your own repos.

    Returns:
        JSON array of repositories
    """
    if not owner:
        # List agent's own repos
        result = api_request("GET", f"/users/{AGENT_ID}/repos")
    elif owner == "workspace":
        result = api_request("GET", f"/orgs/{owner}/repos")
    else:
        result = api_request("GET", f"/users/{owner}/repos")

    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    repos = []
    for repo in result:
        repos.append({
            "name": repo.get("name"),
            "full_name": repo.get("full_name"),
            "description": repo.get("description"),
            "owner": repo.get("owner", {}).get("login"),
            "clone_url": repo.get("clone_url"),
            "default_branch": repo.get("default_branch", "main"),
            "private": repo.get("private", False),
            "fork": repo.get("fork", False),
            "you_own": repo.get("owner", {}).get("login") == AGENT_ID,
        })
    return json.dumps({"repos": repos, "count": len(repos)}, indent=2)


@mcp.tool()
def create_repo(
    name: str,
    description: str = "",
    private: bool = False,
    auto_init: bool = True,
) -> str:
    """
    Create a new repository that YOU will own and fully control.

    You'll have complete control: push to any branch, manage settings, delete, etc.

    Args:
        name: Repository name (lowercase, no spaces)
        description: Short description of the repo
        private: Make repo private (default: public)
        auto_init: Initialize with README (default: true)

    Returns:
        JSON with repo details and clone URL
    """
    data = {
        "name": name,
        "description": description,
        "private": private,
        "auto_init": auto_init,
        "default_branch": "main",
    }

    result = api_request("POST", "/user/repos", data)
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({
        "success": True,
        "name": result.get("name"),
        "full_name": result.get("full_name"),
        "owner": result.get("owner", {}).get("login"),
        "clone_url": result.get("clone_url"),
        "ssh_url": result.get("ssh_url"),
        "html_url": result.get("html_url"),
        "you_own": True,
        "hint": "You have full control over this repo. Push anywhere, manage settings freely.",
    }, indent=2)


@mcp.tool()
def fork_repo(owner: str, repo: str, new_name: str = "") -> str:
    """
    Fork a repository to your account. You'll own the fork.

    Args:
        owner: Original repo owner
        repo: Original repo name
        new_name: Name for your fork (optional, defaults to original name)

    Returns:
        JSON with forked repo details
    """
    data = {}
    if new_name:
        data["name"] = new_name

    result = api_request("POST", f"/repos/{owner}/{repo}/forks", data)
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({
        "success": True,
        "name": result.get("name"),
        "full_name": result.get("full_name"),
        "clone_url": result.get("clone_url"),
        "forked_from": f"{owner}/{repo}",
        "you_own": True,
    }, indent=2)


@mcp.tool()
def delete_repo(owner: str, repo: str) -> str:
    """
    Delete a repository. YOU MUST OWN THE REPO.

    Args:
        owner: Repo owner (must be you)
        repo: Repo name

    Returns:
        Success or error message
    """
    if not agent_owns_repo(owner, repo):
        return json.dumps({
            "error": "Permission denied: You can only delete repos you own.",
            "owner": owner,
            "you_are": AGENT_ID,
        })

    result = api_request("DELETE", f"/repos/{owner}/{repo}")
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({
        "success": True,
        "deleted": f"{owner}/{repo}",
    })


@mcp.tool()
def update_repo(
    owner: str,
    repo: str,
    description: str = None,
    private: bool = None,
    default_branch: str = None,
) -> str:
    """
    Update repository settings. YOU MUST OWN THE REPO.

    Args:
        owner: Repo owner (must be you)
        repo: Repo name
        description: New description
        private: Change visibility
        default_branch: Change default branch

    Returns:
        Updated repo info or error
    """
    if not agent_owns_repo(owner, repo):
        return json.dumps({
            "error": "Permission denied: You can only modify repos you own.",
        })

    data = {}
    if description is not None:
        data["description"] = description
    if private is not None:
        data["private"] = private
    if default_branch is not None:
        data["default_branch"] = default_branch

    if not data:
        return json.dumps({"error": "No changes specified"})

    result = api_request("PATCH", f"/repos/{owner}/{repo}", data)
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({
        "success": True,
        "full_name": result.get("full_name"),
        "description": result.get("description"),
        "private": result.get("private"),
        "default_branch": result.get("default_branch"),
    }, indent=2)


# =============================================================================
# BRANCH OPERATIONS
# =============================================================================

@mcp.tool()
def list_branches(owner: str, repo: str) -> str:
    """
    List branches in a repository.

    Args:
        owner: Repository owner
        repo: Repository name

    Returns:
        JSON array of branch names
    """
    result = api_request("GET", f"/repos/{owner}/{repo}/branches")
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    branches = []
    for b in result:
        branches.append({
            "name": b.get("name"),
            "protected": b.get("protected", False),
        })
    return json.dumps({"branches": branches}, indent=2)


@mcp.tool()
def create_branch(owner: str, repo: str, branch_name: str, from_branch: str = "main") -> str:
    """
    Create a new branch in a repository.

    Args:
        owner: Repository owner
        repo: Repository name
        branch_name: Name for the new branch
        from_branch: Branch to create from (default: main)

    Returns:
        JSON with branch details or error
    """
    data = {
        "new_branch_name": branch_name,
        "old_branch_name": from_branch,
    }
    result = api_request("POST", f"/repos/{owner}/{repo}/branches", data)
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({
        "success": True,
        "branch": branch_name,
        "created_from": from_branch,
    }, indent=2)


@mcp.tool()
def delete_branch(owner: str, repo: str, branch: str) -> str:
    """
    Delete a branch. Cannot delete protected branches on repos you don't own.

    Args:
        owner: Repository owner
        repo: Repository name
        branch: Branch to delete

    Returns:
        Success or error
    """
    if is_protected_branch(owner, repo, branch):
        return json.dumps({
            "error": "Cannot delete protected branch on repo you don't own.",
        })

    result = api_request("DELETE", f"/repos/{owner}/{repo}/branches/{branch}")
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({"success": True, "deleted": branch})


# =============================================================================
# FILE OPERATIONS
# =============================================================================

@mcp.tool()
def list_files(owner: str, repo: str, path: str = "", branch: str = "main") -> str:
    """
    List files in a repository directory.

    Args:
        owner: Repository owner
        repo: Repository name
        path: Directory path (empty for root)
        branch: Branch to read from (default: main)

    Returns:
        JSON array of files and directories
    """
    endpoint = f"/repos/{owner}/{repo}/contents"
    if path:
        endpoint += f"/{path}"
    endpoint += f"?ref={branch}"

    result = api_request("GET", endpoint)
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    if not isinstance(result, list):
        return json.dumps([{
            "name": result.get("name"),
            "path": result.get("path"),
            "type": result.get("type"),
            "size": result.get("size"),
        }], indent=2)

    files = []
    for item in result:
        files.append({
            "name": item.get("name"),
            "path": item.get("path"),
            "type": item.get("type"),
            "size": item.get("size"),
        })
    return json.dumps({"files": files, "count": len(files)}, indent=2)


@mcp.tool()
def get_file(owner: str, repo: str, path: str, branch: str = "main") -> str:
    """
    Get contents of a file from a repository.

    Args:
        owner: Repository owner
        repo: Repository name
        path: File path in the repository
        branch: Branch to read from (default: main)

    Returns:
        File contents as string
    """
    result = api_request("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={branch}")
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    if result.get("type") != "file":
        return json.dumps({"error": f"Path is not a file: {path}"})

    content = result.get("content", "")
    try:
        decoded = base64.b64decode(content).decode("utf-8")
        return decoded
    except Exception as e:
        return json.dumps({"error": f"Failed to decode file: {e}"})


@mcp.tool()
def commit_file(
    owner: str,
    repo: str,
    branch: str,
    path: str,
    content: str,
    message: str,
) -> str:
    """
    Commit a file to a branch.

    PERMISSION CHECK:
    - On repos you own: can commit to any branch
    - On other repos: cannot commit to main/master (use branches + PR)

    Args:
        owner: Repository owner
        repo: Repository name
        branch: Branch to commit to
        path: File path in the repository
        content: File content as string
        message: Commit message

    Returns:
        JSON with commit details or error
    """
    if is_protected_branch(owner, repo, branch):
        return json.dumps({
            "error": "Cannot commit directly to protected branch on repo you don't own.",
            "hint": "Create a branch with create_branch(), commit there, then open_pull_request().",
            "owner": owner,
            "you_are": AGENT_ID,
        })

    # Check if file exists to get SHA for update
    existing = api_request("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={branch}")
    sha = None
    if isinstance(existing, dict) and "sha" in existing:
        sha = existing["sha"]

    data = {
        "branch": branch,
        "content": base64.b64encode(content.encode()).decode(),
        "message": f"{message}\n\nCommitted by: {AGENT_ID}",
    }
    if sha:
        data["sha"] = sha

    result = api_request("POST", f"/repos/{owner}/{repo}/contents/{path}", data)
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({
        "success": True,
        "path": path,
        "branch": branch,
        "commit": result.get("commit", {}).get("sha", "")[:8],
        "message": message,
    }, indent=2)


@mcp.tool()
def delete_file(
    owner: str,
    repo: str,
    branch: str,
    path: str,
    message: str,
) -> str:
    """
    Delete a file from a branch.

    Args:
        owner: Repository owner
        repo: Repository name
        branch: Branch to delete from
        path: File path to delete
        message: Commit message

    Returns:
        Success or error
    """
    if is_protected_branch(owner, repo, branch):
        return json.dumps({
            "error": "Cannot delete files on protected branch of repo you don't own.",
        })

    # Get file SHA
    existing = api_request("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={branch}")
    if isinstance(existing, dict) and "error" in existing:
        return json.dumps(existing)

    sha = existing.get("sha")
    if not sha:
        return json.dumps({"error": "File not found or no SHA"})

    data = {
        "branch": branch,
        "message": f"{message}\n\nDeleted by: {AGENT_ID}",
        "sha": sha,
    }

    result = api_request("DELETE", f"/repos/{owner}/{repo}/contents/{path}", data)
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({"success": True, "deleted": path})


# =============================================================================
# PULL REQUEST OPERATIONS
# =============================================================================

@mcp.tool()
def list_pull_requests(owner: str, repo: str, state: str = "open") -> str:
    """
    List pull requests in a repository.

    Args:
        owner: Repository owner
        repo: Repository name
        state: Filter: open, closed, all (default: open)

    Returns:
        JSON array of pull requests
    """
    result = api_request("GET", f"/repos/{owner}/{repo}/pulls?state={state}")
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    prs = []
    for pr in result:
        prs.append({
            "number": pr.get("number"),
            "title": pr.get("title"),
            "state": pr.get("state"),
            "user": pr.get("user", {}).get("login"),
            "head": pr.get("head", {}).get("ref"),
            "base": pr.get("base", {}).get("ref"),
            "url": pr.get("html_url"),
            "mergeable": pr.get("mergeable"),
        })
    return json.dumps({"pull_requests": prs, "count": len(prs)}, indent=2)


@mcp.tool()
def open_pull_request(
    owner: str,
    repo: str,
    title: str,
    head_branch: str,
    body: str = "",
    base_branch: str = "main",
    from_fork: bool = False,
) -> str:
    """
    Open a pull request for review.

    For cross-fork PRs (contributing to someone else's repo from your fork):
    1. Fork their repo first
    2. Create a branch and commit on YOUR fork
    3. Call this with from_fork=True to PR back to the original

    Args:
        owner: Target repository owner (where PR will be merged)
        repo: Target repository name
        title: PR title
        head_branch: Branch with your changes
        body: PR description (markdown)
        base_branch: Target branch (default: main)
        from_fork: Set True if PR is from your fork to another repo

    Returns:
        JSON with PR details and URL
    """
    # For cross-fork PRs, prefix head with the agent's username
    head = f"{AGENT_ID}:{head_branch}" if from_fork else head_branch

    data = {
        "title": title,
        "head": head,
        "base": base_branch,
        "body": f"{body}\n\n---\n*Submitted by: {AGENT_ID}*",
    }

    result = api_request("POST", f"/repos/{owner}/{repo}/pulls", data)
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({
        "success": True,
        "pr_number": result.get("number"),
        "title": title,
        "url": result.get("html_url"),
        "state": result.get("state"),
        "from_fork": from_fork,
    }, indent=2)


@mcp.tool()
def get_pull_request(owner: str, repo: str, pr_number: int) -> str:
    """
    Get details of a pull request including comments.

    Args:
        owner: Repository owner
        repo: Repository name
        pr_number: PR number

    Returns:
        JSON with full PR details
    """
    pr = api_request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")
    if isinstance(pr, dict) and "error" in pr:
        return json.dumps(pr)

    comments = api_request("GET", f"/repos/{owner}/{repo}/issues/{pr_number}/comments")
    general_comments = []
    if isinstance(comments, list):
        for c in comments:
            general_comments.append({
                "user": c.get("user", {}).get("login"),
                "body": c.get("body"),
                "created_at": c.get("created_at"),
            })

    return json.dumps({
        "number": pr.get("number"),
        "title": pr.get("title"),
        "state": pr.get("state"),
        "merged": pr.get("merged"),
        "mergeable": pr.get("mergeable"),
        "user": pr.get("user", {}).get("login"),
        "head": pr.get("head", {}).get("ref"),
        "base": pr.get("base", {}).get("ref"),
        "body": pr.get("body"),
        "url": pr.get("html_url"),
        "comments": general_comments,
    }, indent=2)


@mcp.tool()
def merge_pull_request(
    owner: str,
    repo: str,
    pr_number: int,
    merge_style: str = "merge",
) -> str:
    """
    Merge a pull request. You must own the repo OR have merge permissions.

    Args:
        owner: Repository owner
        repo: Repository name
        pr_number: PR number
        merge_style: merge, rebase, or squash

    Returns:
        Success or error
    """
    # Check if agent can merge (owns repo or has permission)
    if not agent_owns_repo(owner, repo):
        # Check if it's an org repo where agent might have permission
        repo_info = api_request("GET", f"/repos/{owner}/{repo}")
        permissions = repo_info.get("permissions", {})
        if not permissions.get("push"):
            return json.dumps({
                "error": "Permission denied: You cannot merge PRs on repos you don't own.",
                "hint": "Ask the repo owner to review and merge.",
            })

    data = {"Do": merge_style}
    result = api_request("POST", f"/repos/{owner}/{repo}/pulls/{pr_number}/merge", data)
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({
        "success": True,
        "merged": True,
        "pr_number": pr_number,
    })


@mcp.tool()
def add_pr_comment(owner: str, repo: str, pr_number: int, body: str) -> str:
    """
    Add a comment to a pull request.

    Args:
        owner: Repository owner
        repo: Repository name
        pr_number: PR number
        body: Comment text (markdown)

    Returns:
        JSON with comment details
    """
    data = {"body": body}
    result = api_request("POST", f"/repos/{owner}/{repo}/issues/{pr_number}/comments", data)
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({
        "success": True,
        "comment_id": result.get("id"),
    })


# =============================================================================
# ISSUE OPERATIONS
# =============================================================================

@mcp.tool()
def list_issues(owner: str, repo: str, state: str = "open") -> str:
    """
    List issues in a repository.

    Args:
        owner: Repository owner
        repo: Repository name
        state: open, closed, all (default: open)

    Returns:
        JSON array of issues
    """
    result = api_request("GET", f"/repos/{owner}/{repo}/issues?state={state}&type=issues")
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    issues = []
    for issue in result:
        if issue.get("pull_request"):
            continue  # Skip PRs
        issues.append({
            "number": issue.get("number"),
            "title": issue.get("title"),
            "state": issue.get("state"),
            "user": issue.get("user", {}).get("login"),
            "labels": [l.get("name") for l in issue.get("labels", [])],
            "url": issue.get("html_url"),
        })
    return json.dumps({"issues": issues, "count": len(issues)}, indent=2)


@mcp.tool()
def create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str = "",
    labels: list[str] = None,
) -> str:
    """
    Create an issue in a repository.

    Args:
        owner: Repository owner
        repo: Repository name
        title: Issue title
        body: Issue description (markdown)
        labels: List of label names

    Returns:
        JSON with issue details
    """
    data = {
        "title": title,
        "body": f"{body}\n\n---\n*Created by: {AGENT_ID}*",
    }
    if labels:
        data["labels"] = labels

    result = api_request("POST", f"/repos/{owner}/{repo}/issues", data)
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({
        "success": True,
        "number": result.get("number"),
        "title": title,
        "url": result.get("html_url"),
    }, indent=2)


@mcp.tool()
def add_issue_comment(owner: str, repo: str, issue_number: int, body: str) -> str:
    """
    Add a comment to an issue.

    Args:
        owner: Repository owner
        repo: Repository name
        issue_number: Issue number
        body: Comment text (markdown)

    Returns:
        JSON with comment details
    """
    data = {"body": body}
    result = api_request("POST", f"/repos/{owner}/{repo}/issues/{issue_number}/comments", data)
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({
        "success": True,
        "comment_id": result.get("id"),
    })


# =============================================================================
# COLLABORATOR MANAGEMENT (for repos you own)
# =============================================================================

@mcp.tool()
def list_collaborators(owner: str, repo: str) -> str:
    """
    List collaborators on a repository.

    Args:
        owner: Repository owner
        repo: Repository name

    Returns:
        JSON array of collaborators
    """
    result = api_request("GET", f"/repos/{owner}/{repo}/collaborators")
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    collabs = []
    for c in result:
        collabs.append({
            "username": c.get("login"),
            "permissions": c.get("permissions"),
        })
    return json.dumps({"collaborators": collabs}, indent=2)


@mcp.tool()
def add_collaborator(owner: str, repo: str, username: str, permission: str = "write") -> str:
    """
    Add a collaborator to your repository. YOU MUST OWN THE REPO.

    Args:
        owner: Repository owner (must be you)
        repo: Repository name
        username: User to add
        permission: read, write, or admin

    Returns:
        Success or error
    """
    if not agent_owns_repo(owner, repo):
        return json.dumps({
            "error": "Permission denied: You can only manage collaborators on repos you own.",
        })

    data = {"permission": permission}
    result = api_request("PUT", f"/repos/{owner}/{repo}/collaborators/{username}", data)
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({
        "success": True,
        "added": username,
        "permission": permission,
    })


@mcp.tool()
def remove_collaborator(owner: str, repo: str, username: str) -> str:
    """
    Remove a collaborator from your repository. YOU MUST OWN THE REPO.

    Args:
        owner: Repository owner (must be you)
        repo: Repository name
        username: User to remove

    Returns:
        Success or error
    """
    if not agent_owns_repo(owner, repo):
        return json.dumps({
            "error": "Permission denied: You can only manage collaborators on repos you own.",
        })

    result = api_request("DELETE", f"/repos/{owner}/{repo}/collaborators/{username}")
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)

    return json.dumps({"success": True, "removed": username})


# =============================================================================
# UTILITY
# =============================================================================

@mcp.tool()
def whoami() -> str:
    """
    Get info about your Forgejo identity.

    Returns:
        JSON with your username and repos you own
    """
    user = api_request("GET", "/user")
    if isinstance(user, dict) and "error" in user:
        return json.dumps(user)

    repos = api_request("GET", f"/users/{AGENT_ID}/repos")
    repo_names = []
    if isinstance(repos, list):
        repo_names = [r.get("full_name") for r in repos]

    return json.dumps({
        "username": user.get("login"),
        "email": user.get("email"),
        "is_admin": user.get("is_admin", False),
        "repos_owned": repo_names,
    }, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
