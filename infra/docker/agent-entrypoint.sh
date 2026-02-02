#!/bin/bash
set -e

# Copy credentials from mounted read-only location to writable home
if [ -f "/claude-auth/.credentials.json" ]; then
    cp /claude-auth/.credentials.json /home/agent/.claude/.credentials.json
    echo "Credentials copied successfully"
else
    echo "Warning: No credentials found at /claude-auth/.credentials.json"
    echo "Mount your ~/.claude/.credentials.json to /claude-auth/.credentials.json"
fi

# Run the agent runner
exec python -m src.runner.runner "$@"
