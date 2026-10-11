import logging
import os
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class CacheManager:
    def __init__(self, client: Redis):
        self.client = client

    @staticmethod
    def key(tenant_id: str, namespace: str, key_suffix: str) -> str:
        return f"tenant:{tenant_id}:{namespace}:{key_suffix}"

    async def get(self, key: str) -> str | None:
        try:
            return await self.client.get(key)
        except RedisError:
            logger.warning("Cache read failed", exc_info=True)
            return None

    async def set(self, key: str, value: str, *, ex: int) -> None:
        try:
            await self.client.set(key, value, ex=ex)
        except RedisError:
            logger.warning("Cache write failed", exc_info=True)

    async def invalidate_tenant_namespace(self, tenant_id: str, namespace: str) -> None:
        pattern = self.key(tenant_id, namespace, "*")
        try:
            async for key in self.client.scan_iter(match=pattern, count=200):
                await self.client.unlink(key)
        except RedisError:
            logger.warning("Cache invalidation failed", exc_info=True)


redis_client = Redis.from_url(
    os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True,
    socket_connect_timeout=1, socket_timeout=1,
)
cache_manager = CacheManager(redis_client)


async def close_redis() -> None:
    await redis_client.aclose()
