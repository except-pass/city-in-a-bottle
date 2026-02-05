-- Agent Economy Database Schema

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Token Transaction Ledger
-- All token movements, append-only. Every token movement is a row.
-- Transfers create two rows (out + in). Balance is denormalized for fast lookups.
CREATE TABLE token_transactions (
    tx_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Who
    agent_id        TEXT NOT NULL,
    counterparty_id TEXT,              -- other agent, 'system', 'customer', NULL for debits

    -- What
    tx_type         TEXT NOT NULL,     -- 'debit', 'credit', 'transfer_out', 'transfer_in'
    amount          INTEGER NOT NULL,  -- always positive
    balance_after   INTEGER NOT NULL,  -- agent's balance after this tx

    -- Context
    reason          TEXT NOT NULL,     -- 'run_cost', 'job_reward', 'transfer', 'initial_endowment', 'faucet'
    run_id          UUID,              -- links to agent_runs
    job_id          UUID,              -- if job-related
    note            TEXT,              -- free-form context

    -- Constraints
    CONSTRAINT positive_amount CHECK (amount > 0),
    CONSTRAINT valid_tx_type CHECK (tx_type IN ('debit', 'credit', 'transfer_out', 'transfer_in'))
);

CREATE INDEX idx_tx_agent ON token_transactions(agent_id, timestamp);
CREATE INDEX idx_tx_time ON token_transactions(timestamp);
CREATE INDEX idx_tx_run ON token_transactions(run_id);
CREATE INDEX idx_tx_job ON token_transactions(job_id);

-- Agent Run Log
-- One row per agent run. Captures what they saw, what they did, what they produced.
CREATE TABLE agent_runs (
    run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,

    -- Token accounting
    tokens_in       INTEGER,           -- input tokens consumed
    tokens_out      INTEGER,           -- output tokens generated
    tokens_total    INTEGER,           -- total spend (however you price it)

    -- What they saw
    messages_read   JSONB,             -- [{msg_id, subject, from_agent}]

    -- What they did
    actions         JSONB,             -- [{type, target, detail}]
    /*
      action types:
        - post_message {subject, msg_id, content_summary}
        - bid {job_id, amount}
        - execute_work {job_id}
        - create_tool {name, path}
        - modify_self {file, change_summary}
        - transfer {to_agent, amount, reason}
        - idle {}
    */

    -- What they produced
    artifacts       JSONB,             -- [{type, path_or_id, description}]

    -- Their thinking (gold for sociology)
    reasoning       TEXT,              -- short summary of decision rationale

    -- Outcome
    status          TEXT DEFAULT 'completed',  -- 'completed', 'error', 'bankrupt'
    error_message   TEXT,

    -- Constraints
    CONSTRAINT valid_status CHECK (status IN ('completed', 'error', 'bankrupt', 'running'))
);

CREATE INDEX idx_runs_agent ON agent_runs(agent_id, started_at);
CREATE INDEX idx_runs_time ON agent_runs(started_at);
CREATE INDEX idx_runs_status ON agent_runs(status);

-- Jobs table for tracking job lifecycle
CREATE TABLE jobs (
    job_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Job details
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    reward          INTEGER NOT NULL,
    tags            TEXT[],
    deadline        TIMESTAMPTZ,

    -- Parties involved
    customer_id     TEXT,                          -- agent_id of who posted the job (client)
    assigned_agent  TEXT,                          -- agent_id of who is doing the work

    -- Status tracking
    status          TEXT NOT NULL DEFAULT 'open',  -- 'open', 'in_progress', 'submitted', 'accepted', 'rejected'
    submitted_at    TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,

    -- Message board references
    job_msg_id      UUID,              -- msg_id of the original job posting
    work_msg_id     UUID,              -- msg_id of the submitted work

    -- Constraints
    CONSTRAINT positive_reward CHECK (reward > 0),
    CONSTRAINT valid_job_status CHECK (status IN ('open', 'in_progress', 'submitted', 'accepted', 'rejected', 'cancelled'))
);

CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_agent ON jobs(assigned_agent);
CREATE INDEX idx_jobs_customer ON jobs(customer_id);
CREATE INDEX idx_jobs_created ON jobs(created_at);

-- Agent balances view for quick balance lookups
CREATE VIEW agent_balances AS
SELECT
    agent_id,
    COALESCE(
        (SELECT balance_after
         FROM token_transactions t2
         WHERE t2.agent_id = t1.agent_id
         ORDER BY timestamp DESC
         LIMIT 1),
        0
    ) as balance,
    COUNT(*) as total_transactions,
    MIN(timestamp) as first_transaction,
    MAX(timestamp) as last_transaction
FROM token_transactions t1
GROUP BY agent_id;

-- Function to get current balance for an agent
CREATE OR REPLACE FUNCTION get_agent_balance(p_agent_id TEXT)
RETURNS INTEGER AS $$
DECLARE
    v_balance INTEGER;
BEGIN
    SELECT balance_after INTO v_balance
    FROM token_transactions
    WHERE agent_id = p_agent_id
    ORDER BY timestamp DESC
    LIMIT 1;

    RETURN COALESCE(v_balance, 0);
END;
$$ LANGUAGE plpgsql;

-- Epoch tracking
-- Each epoch is a cycle where all agents get faucet tokens and run their turns.
-- Think of epochs as "days" in the agent economy.
CREATE TABLE epochs (
    epoch_number    INTEGER PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,

    -- Configuration for this epoch
    faucet_amount   INTEGER NOT NULL,         -- tokens given to each agent
    max_turns       INTEGER NOT NULL,         -- max turns per agent

    -- Results
    agents_run      INTEGER,                  -- how many agents participated
    total_faucet    INTEGER,                  -- total tokens distributed
    git_commit      TEXT,                     -- git commit hash at epoch start

    -- Status
    status          TEXT NOT NULL DEFAULT 'running',  -- 'running', 'completed', 'failed'
    error_message   TEXT,

    CONSTRAINT valid_epoch_status CHECK (status IN ('running', 'completed', 'failed'))
);

-- Epoch participation log
-- Tracks each agent's participation in each epoch
CREATE TABLE epoch_participation (
    epoch_number    INTEGER NOT NULL REFERENCES epochs(epoch_number),
    agent_id        TEXT NOT NULL,

    -- Faucet
    faucet_received INTEGER NOT NULL,
    balance_before  INTEGER NOT NULL,
    balance_after   INTEGER,

    -- Activity
    turns_used      INTEGER,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,

    -- Outcome
    status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending', 'running', 'completed', 'failed', 'skipped'
    error_message   TEXT,

    PRIMARY KEY (epoch_number, agent_id),
    CONSTRAINT valid_participation_status CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped'))
);

CREATE INDEX idx_epoch_participation_agent ON epoch_participation(agent_id);
