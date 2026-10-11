"""Create missing tables for a first installation or this additive upgrade.

For future column changes, add reviewed migrations instead of changing tables
implicitly during application startup.
"""
import asyncio
from app.database import engine
from app.models import Base
from app.shop import bootstrap_store


async def main():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await bootstrap_store()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
