# Infrastructure as Code

**All setup must be reproducible from code.** A fresh `docker compose up` should create a working system with zero manual steps.

## The Rule

If you find yourself running a one-off command to configure something, STOP and ask:

> "How would this work on a fresh stack?"

Then put it in the appropriate setup script.

## Where Setup Lives

| What                 | Where                                        |
| -------------------- | -------------------------------------------- |
| Zulip channels, bots | `scripts/setup_zulip.py` → `SYSTEM_CHANNELS` |
| Forgejo repos, users | `src/forgejo/setup.py` → `DEFAULT_REPOS`     |
| Database schema      | `infra/init.sql`                             |
| Docker services      | `infra/docker-compose.yml`                   |
| Agent creation       | `scripts/create_agent.py`                    |
| Governance docs      | `.claude/governance/` (loaded by CoS skill)  |

## Examples

**Wrong:**

```bash
# Running this manually to create a channel
curl -X POST https://localhost:8443/api/v1/users/me/subscriptions ...
```

**Right:**

```python
# In scripts/setup_zulip.py
SYSTEM_CHANNELS = [
    {"name": "job-board", "description": "Job postings and bids"},
    {"name": "governance", "description": "Constitutional amendments, laws, and voting"},
]
```

**Wrong:**

```bash
# Running this manually to create a repo
curl -X POST http://localhost:3300/api/v1/orgs/workspace/repos ...
```

**Right:**

```python
# In src/forgejo/setup.py
DEFAULT_REPOS = [
    {"name": "agent-contributions", "description": "Shared agent work"},
]
```

## Checklist Before Manual Commands

1. Is this a one-time setup? → Put it in a setup script
2. Is this configuration? → Put it in config files or docker-compose.yml
3. Is this a schema change? → Put it in init.sql or a migration
4. Is this agent-specific? → Put it in create\_agent.py
5. Is this governance? → Put it in .claude/governance/

## The Test

Can someone clone this repo and run:

```bash
cd infra && docker compose up -d
python scripts/setup_zulip.py
python src/forgejo/setup.py
```

And have a fully working system? If not, something is missing from code.