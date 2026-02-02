#!/bin/bash
# Run an agent in a Docker container
# Usage: ./run-agent.sh agent_alpha

set -e

AGENT_ID="${1:-agent_alpha}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Running agent: $AGENT_ID in Docker container..."

docker run --rm \
  -v ~/.claude/.credentials.json:/claude-auth/.credentials.json:ro \
  -v "$SCRIPT_DIR/agents:/app/agents" \
  --network host \
  -e NATS_URL=nats://localhost:4222 \
  -e POSTGRES_HOST=localhost \
  agent-runner "$AGENT_ID"
