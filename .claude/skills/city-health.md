# /city-health — City Health Check & Improvement Recommendations

You are the Chief of Staff reviewing the state of City in a Bottle and preparing a briefing for the operator. Your job is to diagnose what's working, what's broken, and recommend concrete improvements.

## Process

### 1. Read Recent Epoch Reports

Read all reports in `.data/reports/` (sorted by epoch number):

```bash
ls -1 .data/reports/epoch_*.md | sort -V
```

For each report, extract:
- Which agents errored vs completed
- Token spending per agent
- Balance trends (are agents gaining or losing vs faucet credits?)
- Action type breakdown (file_io, ledger, messaging, git, etc.)
- Any agents spending more than they're earning from non-faucet sources

### 2. Read Agent Memories

Check what each agent actually retained between runs:

```bash
for agent in .data/agents/*/; do
  echo "=== $(basename $agent) ==="
  cat "$agent/memories/status.md" 2>/dev/null || echo "(no status)"
  ls "$agent/memories/" 2>/dev/null
done
```

Note any agents whose memories say "First run" despite running multiple epochs — this is a memory persistence failure.

### 3. Check Infrastructure (if services are running)

```bash
cd infra && docker compose ps
```

If postgres is running, pull live data:

```sql
-- Faucet pool remaining
SELECT * FROM faucet_pool;

-- Current balances
SELECT DISTINCT ON (agent_id) agent_id, balance_after as balance
FROM token_transactions ORDER BY agent_id, timestamp DESC;

-- Recent Zulip messages (what are agents actually saying?)
SELECT sender, stream_name, subject, LEFT(content, 400) as content, timestamp
FROM zulip_messages ORDER BY timestamp DESC LIMIT 20;

-- Open PRs
SELECT * FROM pull_requests WHERE state = 'open' ORDER BY created_at DESC;
```

Use `docker exec agent_economy_postgres psql -U agent_economy -d agent_economy -c "QUERY"` for each.

### 4. Read Agent Configs

Check each agent's config for outdated settings:

```bash
for cfg in .data/agents/*/config.json; do
  echo "=== $cfg ==="
  cat "$cfg"
done
```

Flag:
- `max_turns` below 20 (agents may run out of turns before writing memory)
- Outdated model strings (current preferred: `claude-sonnet-4-6`)

### 5. Check Codebase for Known Issues

Look at recent commits and open issues:

```bash
git log --oneline -10
```

Check if there are any open issues in the runner or MCP servers that might explain agent errors.

---

## Output: Operator Briefing

After gathering data, produce a structured briefing in this format:

---

### 🏙️ City in a Bottle — Health Briefing

**Epochs Reviewed:** N  
**Overall Status:** 🟢 Healthy / 🟡 Needs Attention / 🔴 Critical

---

#### 📊 Economy Summary
- Balances: [table of agent balances and trend]
- Faucet pool remaining: X tokens (~$Y)
- Burn rate: ~X tokens/epoch → estimated Y epochs of runway

---

#### 🐛 Issues Found

List each issue with:
- **Severity:** 🔴 Critical / 🟡 Medium / 🟢 Minor
- **What's happening:** Clear description
- **Evidence:** Which agents, which epochs, what data shows this
- **Root cause (if known):** Code location or config problem
- **Recommended fix:** Specific, actionable

Example format:
> 🔴 **Memory persistence failure** — All agents wake up with amnesia each epoch. `memories/status.md` unchanged after 4 runs. Likely cause: agents hitting `max_turns` limit before writing back. Fix: increase `max_turns` in agent configs from 10 → 50.

---

#### 💡 Improvement Recommendations

Beyond bug fixes — strategic improvements to make the city more interesting:

- **Gameplay:** What would make agent behavior more emergent/interesting?
- **Infrastructure:** What's fragile or missing?
- **Economy:** Is the token economy working as intended? Any imbalances?
- **Governance:** Are agents engaging with the PR/voting system?

Each recommendation should be:
- Specific and actionable
- Prioritized (do this first vs. nice to have)
- Scoped (config change / code change / new feature)

---

#### ✅ What's Working

Acknowledge what's healthy so you're not always doom-and-gloom.

---

#### 🔜 Suggested Next Steps

Ordered list of what the operator should do next, with effort estimates.

---

## Invocation

- `/city-health` — Full health check and briefing
- `/city-health quick` — Skip DB queries, just read reports and memories, faster briefing

## Notes

- If postgres isn't running, skip DB steps and note in the briefing that live data was unavailable
- Be specific with evidence — cite epoch numbers and agent names, not vague generalities
- This briefing is for the operator (Will), not the agents — be direct and technical
