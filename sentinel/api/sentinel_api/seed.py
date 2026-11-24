"""Seed local Sentinel accounts (responder + viewer) cleanly with Argon2 hashes (ADR-011)."""

from __future__ import annotations

import asyncio
import os

from sentinel_common.config import SentinelSettings
from sentinel_common.db import init_sentinel_schema, make_engine, make_session_factory
from sentinel_common.models import User
from sentinel_common.security import hash_password
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def seed_default_users(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    responder_email: str | None = None,
    responder_password: str | None = None,
    viewer_email: str | None = None,
    viewer_password: str | None = None,
) -> list[str]:
    accounts = [
        (
            (
                responder_email or os.getenv("SEED_RESPONDER_EMAIL") or "responder@sentinel.local"
            ).lower(),
            responder_password or os.getenv("SEED_RESPONDER_PASSWORD") or "responder-dev-pass",
            "responder",
        ),
        (
            (viewer_email or os.getenv("SEED_VIEWER_EMAIL") or "viewer@sentinel.local").lower(),
            viewer_password or os.getenv("SEED_VIEWER_PASSWORD") or "viewer-dev-pass",
            "viewer",
        ),
    ]
    created: list[str] = []
    async with session_factory() as sess:
        for email, pw, role in accounts:
            existing = (
                await sess.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if existing is None:
                sess.add(User(email=email, password_hash=hash_password(pw), role=role))
                created.append(email)
        await sess.commit()
    return created


def main() -> None:
    cfg = SentinelSettings()

    async def _run() -> None:
        engine = make_engine(cfg.database_url)
        await init_sentinel_schema(engine)
        sf = make_session_factory(engine)
        created = await seed_default_users(sf)
        print(f"Seeded accounts: {created or 'already present'}")
        await engine.dispose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
