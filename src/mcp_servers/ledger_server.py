#!/usr/bin/env python3
"""
MCP Server for Agent Economy Token Ledger.

Provides tools for checking balance and transferring tokens.
Run as: python src/mcp/ledger_server.py
"""

import asyncio
import json
import os
import sys
from typing import Optional

from mcp.server import FastMCP

# Add src to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger.client import LedgerClient

# Configuration from environment
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.environ.get("POSTGRES_DB", "agent_economy")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "agent_economy")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "agent_economy_dev")
AGENT_ID = os.environ.get("AGENT_ID", "unknown_agent")

# Create MCP server
mcp = FastMCP(
    name="agent-economy-ledger",
    instructions="Token ledger tools for the agent economy. Use these to check balance and transfer tokens.",
)

# Global client (initialized on first use)
_client: Optional[LedgerClient] = None


async def get_client() -> LedgerClient:
    """Get or create the ledger client."""
    global _client
    if _client is None:
        _client = LedgerClient(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            database=POSTGRES_DB,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
        )
        await _client.connect()
    return _client


@mcp.tool()
async def get_balance() -> str:
    """
    Get your current token balance.

    Returns:
        JSON object with your balance
    """
    try:
        client = await get_client()
        balance = await client.get_balance(AGENT_ID)
        return json.dumps({
            "agent_id": AGENT_ID,
            "balance": balance,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_all_balances() -> str:
    """
    Get token balances for all agents in the economy.

    Returns:
        JSON object mapping agent IDs to balances
    """
    try:
        client = await get_client()
        balances = await client.get_all_balances()
        return json.dumps({
            "balances": balances,
            "total_agents": len(balances),
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def transfer_tokens(
    to_agent: str,
    amount: int,
    reason: str,
) -> str:
    """
    Transfer tokens to another agent.

    Args:
        to_agent: The agent ID to transfer tokens to
        amount: Number of tokens to transfer (must be positive)
        reason: Reason for the transfer

    Returns:
        JSON object with transfer details and new balance
    """
    if amount <= 0:
        return json.dumps({"error": "Amount must be positive"})

    if to_agent == AGENT_ID:
        return json.dumps({"error": "Cannot transfer to yourself"})

    try:
        client = await get_client()
        out_tx, in_tx = await client.transfer(
            AGENT_ID,
            to_agent,
            amount,
            reason,
        )
        return json.dumps({
            "success": True,
            "transferred": amount,
            "to": to_agent,
            "reason": reason,
            "your_new_balance": out_tx.balance_after,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_transactions(limit: int = 10) -> str:
    """
    Get your recent token transactions.

    Args:
        limit: Maximum number of transactions to return (default: 10)

    Returns:
        JSON array of recent transactions
    """
    try:
        client = await get_client()
        txns = await client.get_transactions(AGENT_ID, limit=limit)
        return json.dumps([
            {
                "timestamp": tx.timestamp.isoformat(),
                "type": tx.tx_type,
                "amount": tx.amount,
                "balance_after": tx.balance_after,
                "reason": tx.reason,
                "counterparty": tx.counterparty_id,
            }
            for tx in txns
        ], indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run(transport="stdio")
