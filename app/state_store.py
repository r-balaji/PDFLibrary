"""Minimal shared idempotency for horizontally scaled PDF workers."""

from __future__ import annotations

import logging
from dataclasses import dataclass


REQUEST_TTL_SECONDS = 30 * 60


@dataclass
class ClaimResult:
    state: str
    key: str
    response: dict | None = None


class PostgresIdempotencyStore:
    def __init__(self, database_url: str):
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool
        except ImportError as error:
            raise RuntimeError('Postgres idempotency requires psycopg and psycopg_pool') from error

        self._jsonb = None
        self._pool = AsyncConnectionPool(
            conninfo=database_url,
            min_size=0,
            max_size=4,
            open=False,
            kwargs={'row_factory': dict_row, 'prepare_threshold': None},
        )

    async def start(self) -> None:
        from psycopg.types.json import Jsonb

        self._jsonb = Jsonb
        await self._pool.open(wait=True)
        async with self._pool.connection() as connection:
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pdf_service_request_state (
                    request_key TEXT PRIMARY KEY,
                    payload_hash CHAR(64) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    response_json JSONB,
                    expires_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await connection.execute(
                'DELETE FROM pdf_service_request_state WHERE expires_at <= now()'
            )

    async def close(self) -> None:
        await self._pool.close()

    async def ready(self) -> bool:
        try:
            async with self._pool.connection(timeout=2) as connection:
                row = await (await connection.execute('SELECT 1 AS ready')).fetchone()
                return bool(row and row['ready'] == 1)
        except Exception:
            return False

    async def claim(self, key: str, payload_hash: str) -> ClaimResult:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    'DELETE FROM pdf_service_request_state '
                    'WHERE request_key = %s AND expires_at <= now()',
                    (key,),
                )
                inserted = await (
                    await connection.execute(
                        """
                        INSERT INTO pdf_service_request_state (
                            request_key, payload_hash, status, expires_at
                        )
                        VALUES (
                            %s, %s, 'PROCESSING',
                            now() + (%s * interval '1 second')
                        )
                        ON CONFLICT (request_key) DO NOTHING
                        RETURNING request_key
                        """,
                        (key, payload_hash, REQUEST_TTL_SECONDS),
                    )
                ).fetchone()
                if inserted:
                    return ClaimResult('claimed', key)

                row = await (
                    await connection.execute(
                        'SELECT payload_hash, status, response_json '
                        'FROM pdf_service_request_state WHERE request_key = %s FOR UPDATE',
                        (key,),
                    )
                ).fetchone()
                if row['payload_hash'].strip() != payload_hash and row['status'] != 'FAILED':
                    return ClaimResult('conflict', key)
                if row['status'] == 'COMPLETE' and row['response_json'] is not None:
                    return ClaimResult('complete', key, row['response_json'])
                if row['status'] == 'PROCESSING':
                    return ClaimResult('processing', key)

                await connection.execute(
                    """
                    UPDATE pdf_service_request_state
                    SET payload_hash = %s, status = 'PROCESSING', response_json = NULL,
                        expires_at = now() + (%s * interval '1 second'), updated_at = now()
                    WHERE request_key = %s
                    """,
                    (payload_hash, REQUEST_TTL_SECONDS, key),
                )
                return ClaimResult('claimed', key)

    async def complete(self, key: str, response: dict) -> None:
        async with self._pool.connection() as connection:
            await connection.execute(
                """
                UPDATE pdf_service_request_state
                SET status = 'COMPLETE', response_json = %s,
                    expires_at = now() + (%s * interval '1 second'), updated_at = now()
                WHERE request_key = %s AND status = 'PROCESSING'
                """,
                (self._jsonb(response), REQUEST_TTL_SECONDS, key),
            )

    async def fail(self, key: str) -> None:
        async with self._pool.connection() as connection:
            await connection.execute(
                """
                UPDATE pdf_service_request_state
                SET status = 'FAILED', response_json = NULL,
                    expires_at = now() + (%s * interval '1 second'), updated_at = now()
                WHERE request_key = %s AND status = 'PROCESSING'
                """,
                (REQUEST_TTL_SECONDS, key),
            )


def create_idempotency_store(database_url: str | None):
    if database_url:
        return PostgresIdempotencyStore(database_url)
    logging.getLogger(__name__).warning(
        'DATABASE_URL is not configured; shared request idempotency is disabled'
    )
    return None
