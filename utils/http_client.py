import httpx
import asyncio
import logging
from functools import wraps

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


async def close_client():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


def with_retry(max_retries: int = 2, base_delay: float = 1.0):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                    last_error = e
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(f"Retry {attempt + 1}/{max_retries} after {delay}s: {e}")
                        await asyncio.sleep(delay)
            raise last_error
        return wrapper
    return decorator
