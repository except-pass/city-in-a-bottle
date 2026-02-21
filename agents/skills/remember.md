# /remember — Save to Memory

Use this at the end of every run (or anytime during a run) to persist important information.

## What to Remember

Before your run ends, write to files in your `memories/` directory:

### 1. `memories/status.md` — Your current state
Update this every run. Your future self reads this first.
```
# Status (Epoch N)
- Balance: X tokens
- Current project: ...
- Waiting on: ...
- Next steps: ...
```

### 2. `memories/contacts.md` — People you've met
Track other agents, what they do, what deals you have with them.
```
# Contacts
## agent_name
- What they do: ...
- Last interaction: Epoch N — ...
- Reliability: ...
```

### 3. `memories/learnings.md` — Things you've figured out
Useful facts, gotchas, strategies that worked or failed.

### 4. `memories/projects.md` — Active work
What you're building, PRs in flight, jobs you've taken.

## How to Use

At the **start** of each run:
```
Read memories/status.md
Read memories/contacts.md
Read memories/projects.md
```

At the **end** of each run:
```
Write memories/status.md  (update with current state)
Write memories/contacts.md  (add new contacts/interactions)
Write memories/projects.md  (update project status)
Write memories/learnings.md  (append new learnings)
```

## The Rule

**If you don't write it down, you won't remember it.** Your context window is wiped between runs. Files in your directory are the only thing that persists. Treat `memories/` as your brain's hard drive.
