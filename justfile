# Agent Economy - Task Runner
# Usage: just <recipe>
# Run `just --list` to see all available recipes

set dotenv-load := false

# Default recipe - show help
default:
    @just --list

# =============================================================================
# LIFECYCLE
# =============================================================================

# Full setup: start services, configure Zulip & Forgejo
setup: up wait-healthy setup-zulip setup-forgejo
    @echo "✓ Agent Economy ready!"
    @echo ""
    @echo "Services:"
    @echo "  Zulip:   https://localhost:8443"
    @echo "  Forgejo: http://localhost:3000"
    @echo "  Postgres: localhost:5432"
    @echo ""
    @echo "Create an agent:  just create-agent <name>"
    @echo "Run an agent:     just run <name>"

# Tear down everything (removes all data!)
teardown:
    cd infra && docker compose down -v
    @echo "✓ All services stopped and data removed"

# Restart fresh: teardown + setup
reset: teardown setup

# =============================================================================
# SERVICES
# =============================================================================

# Start all services
up:
    cd infra && docker compose up -d

# Stop all services (keeps data)
down:
    cd infra && docker compose down

# Show service status
status:
    cd infra && docker compose ps

# Show service logs (ctrl-c to exit)
logs *args:
    cd infra && docker compose logs -f {{args}}

# Wait for all services to be healthy
wait-healthy:
    @echo "Waiting for services to be healthy..."
    @timeout 300 bash -c 'until [ "$(docker compose -f infra/docker-compose.yml ps | grep -c healthy)" -ge 3 ]; do sleep 5; done' || (echo "Timeout waiting for services" && exit 1)
    @echo "✓ All services healthy"

# =============================================================================
# SETUP
# =============================================================================

# Configure Zulip (channels, admin)
setup-zulip:
    source .venv/bin/activate && python scripts/setup_zulip.py

# Configure Forgejo (install wizard, org, repos)
setup-forgejo:
    source .venv/bin/activate && python src/forgejo/setup.py

# =============================================================================
# AGENTS
# =============================================================================

# Create a new agent
create-agent name *args:
    source .venv/bin/activate && python scripts/create_agent.py {{name}} {{args}}
    source .venv/bin/activate && python src/forgejo/setup.py --agents {{name}} --skip-install

# Run an agent in the sandbox
run name *args:
    ./run-agent.sh {{name}} {{args}}

# List all agents
list-agents:
    @ls -1 .data/agents/ 2>/dev/null || echo "(no agents)"

# Show agent balances
balances:
    docker exec agent_economy_postgres psql -U agent_economy -d agent_economy -c \
        "SELECT DISTINCT ON (agent_id) agent_id, balance_after as balance FROM token_transactions ORDER BY agent_id, timestamp DESC;"

# =============================================================================
# DATABASE
# =============================================================================

# Open psql shell
db:
    docker exec -it agent_economy_postgres psql -U agent_economy -d agent_economy

# Credit tokens to an agent
credit agent amount reason:
    docker exec agent_economy_postgres psql -U agent_economy -d agent_economy -c \
        "INSERT INTO token_transactions (agent_id, tx_type, amount, balance_after, reason, note) \
         SELECT '{{agent}}', 'credit', {{amount}}, \
           COALESCE((SELECT balance_after FROM token_transactions WHERE agent_id='{{agent}}' ORDER BY timestamp DESC LIMIT 1), 0) + {{amount}}, \
           'manual_credit', '{{reason}}';"

# =============================================================================
# EPOCHS
# =============================================================================

# Run a full epoch (rebuild, faucet, run all agents)
epoch *args:
    source .venv/bin/activate && python scripts/run_epoch.py {{args}}

# Run epoch in dry-run mode (show what would happen)
epoch-dry:
    source .venv/bin/activate && python scripts/run_epoch.py --dry-run

# Show epoch history
epochs:
    docker exec agent_economy_postgres psql -U agent_economy -d agent_economy -c \
        "SELECT epoch_number, started_at::date as date, status, agents_run, total_faucet, git_commit FROM epochs ORDER BY epoch_number DESC LIMIT 10;"

# Show current epoch number
current-epoch:
    @docker exec agent_economy_postgres psql -U agent_economy -d agent_economy -tAc \
        "SELECT COALESCE(MAX(epoch_number), 0) FROM epochs;"

# =============================================================================
# TESTING
# =============================================================================

# Run the test agent to verify all capabilities work
test: _ensure-tester
    ./run-agent.sh agent_tester

# Verify what the test agent accomplished
verify agent="agent_tester":
    source .venv/bin/activate && python scripts/verify_agent_tests.py --agent {{agent}}

# Create test agent from template if it doesn't exist
_ensure-tester:
    @if [ ! -d ".data/agents/agent_tester" ]; then \
        echo "Installing agent_tester from template..."; \
        mkdir -p .data/agents/agent_tester/memories .data/agents/agent_tester/skills; \
        cp agents/agent_tester/agent.md .data/agents/agent_tester/; \
        cp agents/agent_tester/config.json .data/agents/agent_tester/; \
        source .venv/bin/activate && python scripts/setup_zulip.py --agents agent_tester; \
        source .venv/bin/activate && python src/forgejo/setup.py --agents agent_tester --skip-install; \
        echo "✓ agent_tester installed"; \
    fi

# =============================================================================
# MIGRATIONS
# =============================================================================

# Run all pending migrations
migrate:
    @echo "Running migrations..."
    @for f in scripts/migrations/*.sql; do \
        echo "  Running $$f..."; \
        docker exec agent_economy_postgres psql -U agent_economy -d agent_economy -f /dev/stdin < "$$f" 2>&1 | grep -v "already exists" || true; \
    done
    @echo "✓ Migrations complete"

# =============================================================================
# DEV
# =============================================================================

# Install Python dependencies
install-deps:
    python3 -m venv .venv
    source .venv/bin/activate && pip install -r requirements.txt
    source .venv/bin/activate && pip install playwright httpx
    source .venv/bin/activate && playwright install chromium

# Build the agent container
build:
    cd infra && docker compose build agent

# Rebuild agent container (no cache)
rebuild:
    cd infra && docker compose build --no-cache agent
