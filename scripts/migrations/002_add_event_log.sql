-- Migration 002: Add event log for replay visualizations
--
-- This creates a structured, timestamped log of everything that happens
-- in the economy. Perfect for building replay visualizations.

CREATE TABLE IF NOT EXISTS events (
    event_id        BIGSERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Context
    epoch_number    INTEGER,
    run_id          UUID,
    agent_id        TEXT NOT NULL,

    -- Event classification
    event_type      TEXT NOT NULL,  -- 'tool_call', 'tool_result', 'message', 'file_write', 'transfer', 'job_post', 'bid', etc.
    event_subtype   TEXT,           -- More specific: 'channel_message', 'dm', 'read', 'write', etc.

    -- Event data
    tool_name       TEXT,           -- For tool calls
    input           JSONB,          -- Tool input / event parameters
    output          JSONB,          -- Tool output / result

    -- Denormalized fields for fast queries (optional, for common visualizations)
    target_agent    TEXT,           -- For DMs, transfers
    channel         TEXT,           -- For messages
    amount          INTEGER,        -- For transfers

    -- Metadata
    metadata        JSONB           -- Any additional context
);

-- Indexes for common replay queries
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_epoch ON events(epoch_number, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, timestamp);

-- View for easy replay queries
CREATE OR REPLACE VIEW event_timeline AS
SELECT
    event_id,
    timestamp,
    epoch_number,
    agent_id,
    event_type,
    event_subtype,
    tool_name,
    CASE
        WHEN event_type = 'message' THEN (input->>'channel')::text || '/' || (input->>'topic')::text
        WHEN event_type = 'transfer' THEN 'to ' || target_agent || ': ' || amount::text || ' tokens'
        WHEN event_type = 'tool_call' THEN tool_name
        ELSE event_type
    END as summary,
    input,
    output
FROM events
ORDER BY timestamp;

COMMENT ON TABLE events IS 'Structured event log for replay visualizations. Every significant action is logged with timestamp.';
