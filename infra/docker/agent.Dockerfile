# Agent Runner Container
# Runs a sandboxed agent with access to MCP servers

FROM python:3.11-slim

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (required for Claude Code SDK)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Create non-root user for agent
RUN useradd -m -s /bin/bash agent
WORKDIR /app

# Copy requirements and install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/

# Copy entrypoint script
COPY infra/docker/agent-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/agent-entrypoint.sh

# Create directories
RUN mkdir -p /agent && chown agent:agent /agent
RUN mkdir -p /home/agent/.claude && chown -R agent:agent /home/agent

# Switch to non-root user
USER agent

# Environment variables
ENV AGENT_ID=unknown
ENV NATS_URL=nats://nats:4222
ENV POSTGRES_HOST=postgres
ENV POSTGRES_PORT=5432
ENV POSTGRES_DB=agent_economy
ENV POSTGRES_USER=agent_economy
ENV POSTGRES_PASSWORD=agent_economy_dev
ENV PYTHONPATH=/app
ENV HOME=/home/agent

WORKDIR /app

# Entrypoint copies credentials then runs agent
ENTRYPOINT ["/usr/local/bin/agent-entrypoint.sh"]
CMD ["--help"]
