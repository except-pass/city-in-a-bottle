#!/bin/bash
# Run an agent in the sandbox
#
# Usage:
#   ./run-agent.sh agent_beta
#   ./run-agent.sh agent_alpha --max-turns 5
#
# This runs the agent in an isolated Docker container with:
# - Access to Zulip (message board)
# - Access to Forgejo (git)
# - Access to PostgreSQL (ledger)
# - Their own agent directory mounted for persistence
# - Claude credentials mounted read-only

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$1" ]; then
    echo "Usage: $0 <agent_name> [options]"
    echo ""
    echo "Examples:"
    echo "  $0 agent_beta"
    echo "  $0 agent_alpha --max-turns 5"
    echo ""
    echo "Available agents:"
    ls -1 "$SCRIPT_DIR/.data/agents/" 2>/dev/null | grep -v "^_" || echo "  (none found)"
    exit 1
fi

AGENT_NAME="$1"
shift

# Agent data lives in .data/agents/ (gitignored, separate from repo code)
AGENTS_DIR="$SCRIPT_DIR/.data/agents"

# Check agent exists
if [ ! -d "$AGENTS_DIR/$AGENT_NAME" ]; then
    echo "Error: Agent '$AGENT_NAME' not found in .data/agents/"
    echo ""
    echo "Create it with:"
    echo "  python scripts/create_agent.py $AGENT_NAME"
    exit 1
fi

# Check credentials
if [ ! -f "$HOME/.claude/.credentials.json" ]; then
    echo "Error: Claude credentials not found at ~/.claude/.credentials.json"
    echo ""
    echo "Run 'claude' to authenticate first."
    exit 1
fi

# Get current epoch number (if not already set)
if [ -z "$EPOCH_NUMBER" ]; then
    EPOCH_NUMBER=$(docker exec agent_economy_postgres psql -U agent_economy -d agent_economy -tAc \
        "SELECT COALESCE(MAX(epoch_number), 0) FROM epochs;" 2>/dev/null || echo "0")
fi
export EPOCH_NUMBER

echo "Starting $AGENT_NAME in sandbox (epoch $EPOCH_NUMBER)..."
cd "$SCRIPT_DIR/infra"

# Build if needed and run
docker compose run --rm -e EPOCH_NUMBER="$EPOCH_NUMBER" agent "$AGENT_NAME" "$@"
