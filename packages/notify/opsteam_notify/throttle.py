"""Anti-spam: at most one message a minute per incident and channel, and no repeated content."""

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

RATE_S = 60
SEEN_S = 86400

log = structlog.get_logger()


class Throttle:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    @staticmethod
    def _keys(tenant: str, channel: str, incident: str, digest: str) -> tuple[str, str]:
        prefix = f"t:{tenant}:notify:{channel}"
        return f"{prefix}:rate:{incident}", f"{prefix}:seen:{digest}"

    async def allow(self, tenant: str, channel: str, incident: str, digest: str) -> bool:
        """Whether this message may go out now; fails open, as a duplicate beats silence."""
        try:
            return not await self.redis.exists(*self._keys(tenant, channel, incident, digest))
        except RedisError:
            log.warning("notify.throttle_unavailable", tenant_id=tenant)
            return True

    async def sent(self, tenant: str, channel: str, incident: str, digest: str) -> None:
        """Record a delivered message so retries and repeats are held back."""
        rate, seen = self._keys(tenant, channel, incident, digest)
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.set(rate, 1, ex=RATE_S)
                pipe.set(seen, 1, ex=SEEN_S)
                await pipe.execute()
        except RedisError:
            log.warning("notify.throttle_unavailable", tenant_id=tenant)
