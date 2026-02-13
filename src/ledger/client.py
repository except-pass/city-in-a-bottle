"""
Token Ledger Client for City in a Bottle.

Provides interface to Postgres for token accounting operations.
"""

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg


@dataclass
class Transaction:
    """Represents a token transaction."""
    tx_id: UUID
    timestamp: datetime
    agent_id: str
    counterparty_id: Optional[str]
    tx_type: str
    amount: int
    balance_after: int
    reason: str
    run_id: Optional[UUID]
    job_id: Optional[UUID]
    note: Optional[str]


class LedgerClient:
    """Client for token ledger operations."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = int(os.environ.get("POSTGRES_PORT", "5434")),
        database: str = "agent_economy",
        user: str = "agent_economy",
        password: str = "agent_economy_dev",
    ):
        self.dsn = f"postgresql://{user}:{password}@{host}:{port}/{database}"
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Connect to the database."""
        self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10)

    async def close(self) -> None:
        """Close database connection."""
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def get_balance(self, agent_id: str) -> int:
        """
        Get the current token balance for an agent.

        Args:
            agent_id: The agent's ID

        Returns:
            Current balance (can be negative if in debt)
        """
        if not self._pool:
            raise RuntimeError("Not connected to database")

        async with self._pool.acquire() as conn:
            result = await conn.fetchval(
                """
                SELECT balance_after
                FROM token_transactions
                WHERE agent_id = $1
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                agent_id,
            )
            return result if result is not None else 0

    async def create_agent(
        self,
        agent_id: str,
        initial_balance: int,
        note: Optional[str] = None,
    ) -> Transaction:
        """
        Create a new agent with an initial token balance.

        Args:
            agent_id: The agent's ID
            initial_balance: Starting token balance
            note: Optional note

        Returns:
            The initial credit transaction
        """
        if not self._pool:
            raise RuntimeError("Not connected to database")

        async with self._pool.acquire() as conn:
            # Check if agent already has transactions
            existing = await conn.fetchval(
                "SELECT COUNT(*) FROM token_transactions WHERE agent_id = $1",
                agent_id,
            )
            if existing > 0:
                raise ValueError(f"Agent {agent_id} already exists")

            row = await conn.fetchrow(
                """
                INSERT INTO token_transactions
                    (agent_id, counterparty_id, tx_type, amount, balance_after, reason, note)
                VALUES ($1, 'system', 'credit', $2, $2, 'initial_endowment', $3)
                RETURNING *
                """,
                agent_id,
                initial_balance,
                note or f"Initial endowment for {agent_id}",
            )

            return Transaction(**dict(row))

    async def debit(
        self,
        agent_id: str,
        tokens: int,
        reason: str,
        run_id: Optional[UUID] = None,
        note: Optional[str] = None,
    ) -> Transaction:
        """
        Debit tokens from an agent's balance (spending).

        Args:
            agent_id: The agent's ID
            tokens: Amount to debit (positive number)
            reason: Reason for debit (e.g., 'run_cost')
            run_id: Optional associated run ID
            note: Optional note

        Returns:
            The debit transaction
        """
        if tokens <= 0:
            raise ValueError("Tokens must be positive")

        if not self._pool:
            raise RuntimeError("Not connected to database")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Get current balance
                current_balance = await self.get_balance(agent_id)
                new_balance = current_balance - tokens

                row = await conn.fetchrow(
                    """
                    INSERT INTO token_transactions
                        (agent_id, tx_type, amount, balance_after, reason, run_id, note)
                    VALUES ($1, 'debit', $2, $3, $4, $5, $6)
                    RETURNING *
                    """,
                    agent_id,
                    tokens,
                    new_balance,
                    reason,
                    run_id,
                    note,
                )

                return Transaction(**dict(row))

    async def credit(
        self,
        agent_id: str,
        tokens: int,
        reason: str,
        counterparty_id: str = "customer",
        job_id: Optional[UUID] = None,
        note: Optional[str] = None,
    ) -> Transaction:
        """
        Credit tokens to an agent's balance (earning).

        Args:
            agent_id: The agent's ID
            tokens: Amount to credit (positive number)
            reason: Reason for credit (e.g., 'job_reward')
            counterparty_id: Who is providing the tokens
            job_id: Optional associated job ID
            note: Optional note

        Returns:
            The credit transaction
        """
        if tokens <= 0:
            raise ValueError("Tokens must be positive")

        if not self._pool:
            raise RuntimeError("Not connected to database")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Get current balance
                current_balance = await self.get_balance(agent_id)
                new_balance = current_balance + tokens

                row = await conn.fetchrow(
                    """
                    INSERT INTO token_transactions
                        (agent_id, counterparty_id, tx_type, amount, balance_after, reason, job_id, note)
                    VALUES ($1, $2, 'credit', $3, $4, $5, $6, $7)
                    RETURNING *
                    """,
                    agent_id,
                    counterparty_id,
                    tokens,
                    new_balance,
                    reason,
                    job_id,
                    note,
                )

                return Transaction(**dict(row))

    async def transfer(
        self,
        from_agent: str,
        to_agent: str,
        tokens: int,
        reason: str,
        note: Optional[str] = None,
    ) -> tuple[Transaction, Transaction]:
        """
        Transfer tokens from one agent to another.

        Args:
            from_agent: Source agent ID
            to_agent: Destination agent ID
            tokens: Amount to transfer (positive number)
            reason: Reason for transfer
            note: Optional note

        Returns:
            Tuple of (outgoing transaction, incoming transaction)
        """
        if tokens <= 0:
            raise ValueError("Tokens must be positive")

        if from_agent == to_agent:
            raise ValueError("Cannot transfer to self")

        if not self._pool:
            raise RuntimeError("Not connected to database")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Get current balances
                from_balance = await self.get_balance(from_agent)
                to_balance = await self.get_balance(to_agent)

                new_from_balance = from_balance - tokens
                new_to_balance = to_balance + tokens

                # Record outgoing transfer
                out_row = await conn.fetchrow(
                    """
                    INSERT INTO token_transactions
                        (agent_id, counterparty_id, tx_type, amount, balance_after, reason, note)
                    VALUES ($1, $2, 'transfer_out', $3, $4, $5, $6)
                    RETURNING *
                    """,
                    from_agent,
                    to_agent,
                    tokens,
                    new_from_balance,
                    reason,
                    note,
                )

                # Record incoming transfer
                in_row = await conn.fetchrow(
                    """
                    INSERT INTO token_transactions
                        (agent_id, counterparty_id, tx_type, amount, balance_after, reason, note)
                    VALUES ($1, $2, 'transfer_in', $3, $4, $5, $6)
                    RETURNING *
                    """,
                    to_agent,
                    from_agent,
                    tokens,
                    new_to_balance,
                    reason,
                    note,
                )

                return Transaction(**dict(out_row)), Transaction(**dict(in_row))

    async def get_transactions(
        self,
        agent_id: str,
        limit: int = 100,
    ) -> list[Transaction]:
        """
        Get recent transactions for an agent.

        Args:
            agent_id: The agent's ID
            limit: Maximum number of transactions to return

        Returns:
            List of transactions, most recent first
        """
        if not self._pool:
            raise RuntimeError("Not connected to database")

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM token_transactions
                WHERE agent_id = $1
                ORDER BY timestamp DESC
                LIMIT $2
                """,
                agent_id,
                limit,
            )
            return [Transaction(**dict(row)) for row in rows]

    async def get_all_balances(self) -> dict[str, int]:
        """
        Get balances for all agents.

        Returns:
            Dictionary mapping agent_id to balance
        """
        if not self._pool:
            raise RuntimeError("Not connected to database")

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (agent_id)
                    agent_id, balance_after
                FROM token_transactions
                ORDER BY agent_id, timestamp DESC
                """
            )
            return {row["agent_id"]: row["balance_after"] for row in rows}


# CLI support
if __name__ == "__main__":
    import argparse
    import asyncio

    async def main():
        parser = argparse.ArgumentParser(description="Token ledger CLI")
        subparsers = parser.add_subparsers(dest="command", required=True)

        # Create agent
        create_parser = subparsers.add_parser("create-agent", help="Create new agent with balance")
        create_parser.add_argument("agent_id", help="Agent ID")
        create_parser.add_argument("balance", type=int, help="Initial balance")

        # Get balance
        balance_parser = subparsers.add_parser("balance", help="Get agent balance")
        balance_parser.add_argument("agent_id", help="Agent ID")

        # List all balances
        subparsers.add_parser("balances", help="List all agent balances")

        # Debit
        debit_parser = subparsers.add_parser("debit", help="Debit tokens")
        debit_parser.add_argument("agent_id", help="Agent ID")
        debit_parser.add_argument("tokens", type=int, help="Amount")
        debit_parser.add_argument("--reason", default="manual", help="Reason")

        # Credit
        credit_parser = subparsers.add_parser("credit", help="Credit tokens")
        credit_parser.add_argument("agent_id", help="Agent ID")
        credit_parser.add_argument("tokens", type=int, help="Amount")
        credit_parser.add_argument("--reason", default="manual", help="Reason")

        # Transfer
        transfer_parser = subparsers.add_parser("transfer", help="Transfer tokens")
        transfer_parser.add_argument("from_agent", help="Source agent ID")
        transfer_parser.add_argument("to_agent", help="Destination agent ID")
        transfer_parser.add_argument("tokens", type=int, help="Amount")
        transfer_parser.add_argument("--reason", default="transfer", help="Reason")

        # Transactions
        txn_parser = subparsers.add_parser("transactions", help="List transactions")
        txn_parser.add_argument("agent_id", help="Agent ID")
        txn_parser.add_argument("--limit", type=int, default=10, help="Limit")

        args = parser.parse_args()

        async with LedgerClient() as client:
            if args.command == "create-agent":
                tx = await client.create_agent(args.agent_id, args.balance)
                print(f"Created agent {args.agent_id} with balance {args.balance}")
                print(f"Transaction ID: {tx.tx_id}")

            elif args.command == "balance":
                balance = await client.get_balance(args.agent_id)
                print(f"{args.agent_id}: {balance} tokens")

            elif args.command == "balances":
                balances = await client.get_all_balances()
                if balances:
                    print("Agent Balances:")
                    for agent_id, balance in sorted(balances.items()):
                        print(f"  {agent_id}: {balance}")
                else:
                    print("No agents found")

            elif args.command == "debit":
                tx = await client.debit(args.agent_id, args.tokens, args.reason)
                print(f"Debited {args.tokens} from {args.agent_id}")
                print(f"New balance: {tx.balance_after}")

            elif args.command == "credit":
                tx = await client.credit(args.agent_id, args.tokens, args.reason)
                print(f"Credited {args.tokens} to {args.agent_id}")
                print(f"New balance: {tx.balance_after}")

            elif args.command == "transfer":
                out_tx, in_tx = await client.transfer(
                    args.from_agent, args.to_agent, args.tokens, args.reason
                )
                print(f"Transferred {args.tokens} from {args.from_agent} to {args.to_agent}")
                print(f"{args.from_agent} balance: {out_tx.balance_after}")
                print(f"{args.to_agent} balance: {in_tx.balance_after}")

            elif args.command == "transactions":
                txns = await client.get_transactions(args.agent_id, limit=args.limit)
                print(f"Recent transactions for {args.agent_id}:")
                for tx in txns:
                    direction = "+" if tx.tx_type in ("credit", "transfer_in") else "-"
                    print(f"  {tx.timestamp.isoformat()}: {direction}{tx.amount} ({tx.reason}) -> {tx.balance_after}")

    asyncio.run(main())
