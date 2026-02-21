-- Migration 003: Faucet Funding
-- Tracks real-dollar deposits that back the token faucet.

CREATE TABLE IF NOT EXISTS faucet_funding (
    id              SERIAL PRIMARY KEY,
    funded_by       TEXT NOT NULL,
    usd_amount      NUMERIC(10,2) NOT NULL,
    tokens_purchased BIGINT NOT NULL,
    exchange_rate   NUMERIC(10,2) NOT NULL DEFAULT 10.00,
    purpose         TEXT NOT NULL,
    timestamp       TIMESTAMPTZ DEFAULT now(),
    note            TEXT,

    CONSTRAINT valid_purpose CHECK (purpose IN ('faucet', 'personal_purchase')),
    CONSTRAINT positive_usd CHECK (usd_amount > 0),
    CONSTRAINT positive_tokens CHECK (tokens_purchased > 0)
);

CREATE OR REPLACE VIEW faucet_pool AS
SELECT
    COALESCE(SUM(CASE WHEN purpose = 'faucet' THEN tokens_purchased ELSE 0 END), 0) as total_funded,
    (SELECT COALESCE(SUM(amount), 0) FROM token_transactions WHERE reason = 'faucet') as total_distributed,
    COALESCE(SUM(CASE WHEN purpose = 'faucet' THEN tokens_purchased ELSE 0 END), 0) -
    (SELECT COALESCE(SUM(amount), 0) FROM token_transactions WHERE reason = 'faucet') as remaining
FROM faucet_funding;
