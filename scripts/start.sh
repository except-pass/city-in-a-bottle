#!/bin/bash
# One-command startup for the City in a Bottle stack
#
# Usage: ./scripts/start.sh
#
# This script:
# 1. Starts all docker-compose services
# 2. Waits for Zulip to be healthy
# 3. Runs setup_zulip.py to configure channels and bots
#
# The setup is idempotent - safe to run multiple times.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Starting City in a Bottle stack..."
cd "$PROJECT_ROOT/infra"
docker compose up -d

echo "Waiting for Zulip to be healthy..."
until docker inspect agent_economy_zulip --format '{{.State.Health.Status}}' 2>/dev/null | grep -q healthy; do
    echo "  Still waiting..."
    sleep 5
done

echo "Zulip is healthy. Running setup..."
cd "$PROJECT_ROOT"

# Activate virtualenv if it exists
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

python scripts/setup_zulip.py --skip-wait

echo ""
echo "================================================"
echo "City in a Bottle is ready!"
echo ""
echo "  Zulip UI:     https://localhost:${ZULIP_HTTPS_PORT:-8443}"
echo "  Zulip API:    http://localhost:${ZULIP_HTTP_PORT:-8081}"
echo "  Forgejo:      http://localhost:${FORGEJO_PORT:-3300}"
echo "  PostgreSQL:   localhost:${POSTGRES_PORT:-5434}"
echo ""
echo "  Admin login:  admin@agent-economy.local"
echo "  Password:     admin-dev-password-123"
echo "================================================"
