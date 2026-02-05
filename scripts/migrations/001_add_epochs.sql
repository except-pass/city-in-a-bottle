-- Migration 001: Add epoch tracking tables
-- Run this if upgrading from a pre-epoch database

-- Epoch tracking
CREATE TABLE IF NOT EXISTS epochs (
    epoch_number    INTEGER PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,

    -- Configuration for this epoch
    faucet_amount   INTEGER NOT NULL,
    max_turns       INTEGER NOT NULL,

    -- Results
    agents_run      INTEGER,
    total_faucet    INTEGER,
    git_commit      TEXT,

    -- Status
    status          TEXT NOT NULL DEFAULT 'running',
    error_message   TEXT,

    CONSTRAINT valid_epoch_status CHECK (status IN ('running', 'completed', 'failed'))
);

-- Epoch participation log
CREATE TABLE IF NOT EXISTS epoch_participation (
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
    status          TEXT NOT NULL DEFAULT 'pending',
    error_message   TEXT,

    PRIMARY KEY (epoch_number, agent_id),
    CONSTRAINT valid_participation_status CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS idx_epoch_participation_agent ON epoch_participation(agent_id);

-- Also add customer_id to jobs if missing (from earlier bug fix)
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS customer_id TEXT;
CREATE INDEX IF NOT EXISTS idx_jobs_customer ON jobs(customer_id);
