"""Real PostgreSQL 16 (testcontainers) fixtures for Layer 3 integration tests (`make integration`).

Zero SQLite, zero mocked database: spins up an isolated `postgres:16-alpine` container once
per test session, initializes all 4 databases (`orders`, `payments`, `sentinel`, `chaos`) and
least-privilege roles from `platform/dev-shims/postgres/init-dbs.sql`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_SQL = REPO_ROOT / "platform/dev-shims/postgres/init-dbs.sql"


@pytest.fixture(scope="session")
def pg_container() -> Iterator[dict[str, Any]]:
    """Start a real postgres:16-alpine container and apply platform init-dbs.sql."""
    with PostgresContainer(
        "postgres:16-alpine", username="postgres", password="pg-test-superuser", dbname="postgres"
    ) as pg:
        host = pg.get_container_host_ip()
        port = int(pg.get_exposed_port(5432))

        # Apply init-dbs.sql (CREATE DATABASE / CREATE ROLE / REVOKE CONNECT / \connect sentinel grants)
        raw_sql = INIT_SQL.read_text(encoding="utf-8")
        sections: list[tuple[str, list[str]]] = [("postgres", [])]
        for line in raw_sql.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            if stripped.startswith("\\connect "):
                target_db = stripped.split(None, 1)[1].strip().rstrip(";")
                sections.append((target_db, []))
                continue
            sections[-1][1].append(line)

        for target_db, lines in sections:
            block = "\n".join(lines).strip()
            if not block:
                continue
            with psycopg.connect(
                host=host,
                port=port,
                user="postgres",
                password="pg-test-superuser",
                dbname=target_db,
                autocommit=True,
            ) as conn:
                with conn.cursor() as cur:
                    for stmt in block.split(";"):
                        cleaned = stmt.strip()
                        if cleaned:
                            cur.execute(cleaned)

        yield {
            "host": host,
            "port": port,
            "superuser_dsn": f"postgresql+asyncpg://postgres:pg-test-superuser@{host}:{port}/sentinel",
            "chaos_superuser_dsn": f"postgresql+asyncpg://postgres:pg-test-superuser@{host}:{port}/chaos",
            "orders_superuser_dsn": f"postgresql+asyncpg://postgres:pg-test-superuser@{host}:{port}/orders",
            "payments_superuser_dsn": f"postgresql+asyncpg://postgres:pg-test-superuser@{host}:{port}/payments",
        }
