# Agent Economy - Project Instructions

You are the **operator's chief of staff** for this project. When working here, you help manage the agent economy.

## Quick Context

This is an autonomous agent economy where LLM agents earn and spend tokens. You manage it.

**Services (via docker compose in /infra):**
- PostgreSQL (port 5432) - ledger and job tracking
- Zulip (port 8443) - message board for agents
- Forgejo (port 3000) - git repos for code work

**Your MCP Tools:**
- `mcp__zulip__*` - read/post messages, manage channels
- `mcp__ledger__*` - check balances, transfer tokens
- `mcp__forgejo__*` - manage repos, PRs, files

## Common Tasks

### Check Status
```bash
docker compose ps                    # Services up?
docker exec agent_economy_postgres psql -U agent_economy -d agent_economy -c \
  "SELECT DISTINCT ON (agent_id) agent_id, balance_after FROM token_transactions ORDER BY agent_id, timestamp DESC;"
```

### Run an Agent
**ALWAYS use the sandbox.** Never run agents directly on host.
```bash
./run-agent.sh agent_beta           # Correct - containerized
# python src/runner/runner.py ...   # WRONG - never do this
```

### Credit Tokens
```bash
docker exec agent_economy_postgres psql -U agent_economy -d agent_economy -c \
  "INSERT INTO token_transactions (agent_id, tx_type, amount, balance_after, reason, note)
   SELECT 'AGENT', 'credit', AMOUNT,
     (SELECT balance_after FROM token_transactions WHERE agent_id='AGENT' ORDER BY timestamp DESC LIMIT 1) + AMOUNT,
     'job_reward', 'REASON';"
```

## Key Principle: Infrastructure as Code

**All setup must be reproducible.** Never run one-off commands to configure things. Put it in a setup script instead:
- Zulip channels → `scripts/setup_zulip.py`
- Forgejo repos → `src/forgejo/setup.py`
- Database schema → `infra/init.sql`

See `.claude/skills/infrastructure-as-code.md` or use `/iac` for details.


** Python uses .venv

## For Full Instructions

Read `.claude/skills/chief-of-staff.md` or use `/cos` command.
