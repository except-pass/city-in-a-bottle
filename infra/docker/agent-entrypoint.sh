#!/bin/bash
set -e

# Auth: support OAuth token (Max/Pro), API key, or legacy credentials file
if [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
    echo "Using Claude OAuth token (Max/Pro subscription)"
elif [ -n "$ANTHROPIC_API_KEY" ]; then
    echo "Using Anthropic API key"
elif [ -f "/claude-auth/.credentials.json" ]; then
    cp /claude-auth/.credentials.json /home/agent/.claude/.credentials.json
    echo "Using legacy credentials file"
else
    echo "Warning: No Claude credentials found"
    echo "Set CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, or mount credentials file"
fi

# Run the agent runner
exec python -m src.runner.runner "$@"
