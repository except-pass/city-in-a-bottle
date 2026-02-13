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
# - Claude auth (OAuth from Max/Pro, API key, or legacy credentials)

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

# --- Claude Authentication ---
# Priority: ANTHROPIC_API_KEY > ~/.anthropic/api_key > CLAUDE_CODE_OAUTH_TOKEN > keychain auto-extract
if [ -n "$ANTHROPIC_API_KEY" ]; then
    echo "Auth: using ANTHROPIC_API_KEY env var"
elif [ -f "$HOME/.anthropic/api_key" ]; then
    ANTHROPIC_API_KEY=$(cat "$HOME/.anthropic/api_key" | tr -d '[:space:]')
    echo "Auth: using ~/.anthropic/api_key"
elif [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
    echo "Auth: using provided CLAUDE_CODE_OAUTH_TOKEN"
elif command -v security &>/dev/null; then
    # macOS: extract OAuth credentials from keychain and refresh if needed
    KEYCHAIN_DATA=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null || true)
    if [ -n "$KEYCHAIN_DATA" ]; then
        eval "$(echo "$KEYCHAIN_DATA" | python3 -c "
import sys, json, time
d = json.loads(sys.stdin.read()).get('claudeAiOauth', {})
at = d.get('accessToken', '')
rt = d.get('refreshToken', '')
exp = d.get('expiresAt', 0)
now_ms = int(time.time() * 1000)
expired = 'true' if exp < now_ms else 'false'
print(f'_ACCESS_TOKEN=\"{at}\"')
print(f'_REFRESH_TOKEN=\"{rt}\"')
print(f'_EXPIRED={expired}')
" 2>/dev/null)"

        if [ "$_EXPIRED" = "true" ] && [ -n "$_REFRESH_TOKEN" ]; then
            echo "Auth: OAuth token expired, refreshing..."
            REFRESH_RESPONSE=$(curl -s -X POST https://platform.claude.com/v1/oauth/token \
                -H "Content-Type: application/json" \
                -d "{\"grant_type\":\"refresh_token\",\"refresh_token\":\"$_REFRESH_TOKEN\",\"client_id\":\"9d1c250a-e61b-44d9-88ed-5944d1962f5e\"}" 2>/dev/null)

            NEW_TOKEN=$(echo "$REFRESH_RESPONSE" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('access_token',''))" 2>/dev/null)
            if [ -n "$NEW_TOKEN" ]; then
                CLAUDE_CODE_OAUTH_TOKEN="$NEW_TOKEN"
                echo "Auth: token refreshed successfully"
            else
                echo "Auth: OAuth refresh failed, falling back..."
            fi
        elif [ -n "$_ACCESS_TOKEN" ]; then
            CLAUDE_CODE_OAUTH_TOKEN="$_ACCESS_TOKEN"
            echo "Auth: using OAuth token from keychain"
        fi
        unset _ACCESS_TOKEN _REFRESH_TOKEN _EXPIRED KEYCHAIN_DATA
    fi
fi

if [ -z "$CLAUDE_CODE_OAUTH_TOKEN" ] && [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "Error: No Claude credentials found."
    echo ""
    echo "Options:"
    echo "  1. Set ANTHROPIC_API_KEY env var or put key in ~/.anthropic/api_key"
    echo "  2. Log in with 'claude' interactively (Max/Pro - token auto-extracted from keychain)"
    echo "  3. Set CLAUDE_CODE_OAUTH_TOKEN env var"
    exit 1
fi
export ANTHROPIC_API_KEY
export CLAUDE_CODE_OAUTH_TOKEN

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
