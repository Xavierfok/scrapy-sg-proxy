"""Scrapy downloader middleware for a mobile proxy with a rotation link.

Every request goes out through one proxy endpoint. When a site answers with a
blocking status (403 and 429 by default), the middleware calls the rotation
link, waits for the modem to come back on a new IP, and retries the request.

Rotation itself is ``proxy_rotator.MobileProxy`` (the ``proxy-rotator-sg``
package): cooldowns, ``429 Retry-After`` on the rotation link, and polling
until the exit IP actually changes. It blocks, so it runs in a thread and the
reactor keeps going.

Many requests are usually in flight when a block lands, and they all come back
blocked. Only the first one rotates. Each request remembers which "generation"
of IP it was sent on; a blocked response from an older generation is just
retried on the new IP.
"""

import logging
from typing import Any, Callable, Optional

from proxy_rotator import MobileProxy, RotationError
from scrapy.exceptions import NotConfigured
from scrapy.http import Request, Response
from scrapy.utils.defer import maybe_deferred_to_future
from twisted.internet.defer import DeferredLock
from twisted.internet.threads import deferToThread

logger = logging.getLogger(__name__)

META_GENERATION = "sgproxy_generation"
META_RETRIES = "sgproxy_retries"


class SingaporeProxyMiddleware:
    """Route requests through a mobile proxy and rotate its IP when blocked.

    Settings:
        SGPROXY_URL: ``http://user:pass@host:port`` of your proxy. Required.
        SGPROXY_ROTATE_URL: the rotation link. Without it the middleware
            only sets the proxy and never rotates.
        SGPROXY_ROTATE_ON: HTTP statuses that trigger a rotation.
            Default ``[403, 429]``.
        SGPROXY_MAX_RETRIES: rotations tried for one request before the
            blocked response is passed on. Default 2.
        SGPROXY_ROTATE_EVERY: also rotate after this many requests. 0 (the
            default) turns it off.
        SGPROXY_MIN_INTERVAL: seconds to leave between rotations. Default 0.
        SGPROXY_WAIT_FOR_NEW_IP: poll until the exit IP changes before
            retrying. Default True.
        SGPROXY_ROTATE_TIMEOUT: max seconds to wait for the new IP. Default 90.
        SGPROXY_IP_CHECK_URL: URL that returns the caller's IP as text.
            Default ``https://api.ipify.org``.

    Per request, ``meta={"dont_proxy": True}`` skips the proxy, and a
    different ``proxy`` already in ``meta`` is left alone.
    """

    def __init__(
        self,
        proxy: MobileProxy,
        can_rotate: bool = True,
        rotate_on: Optional[set] = None,
        max_retries: int = 2,
        rotate_every: int = 0,
        wait_for_new_ip: bool = True,
        rotate_timeout: float = 90,
        stats: Any = None,
        run_in_thread: Callable[..., Any] = deferToThread,
    ) -> None:
        self.proxy = proxy
        self.can_rotate = can_rotate
        self.rotate_on = set(rotate_on if rotate_on is not None else (403, 429))
        self.max_retries = max_retries
        self.rotate_every = rotate_every
        self.wait_for_new_ip = wait_for_new_ip
        self.rotate_timeout = rotate_timeout
        self.stats = stats
        self._run_in_thread = run_in_thread
        self._lock = DeferredLock()
        self.generation = 0
        self._sent_this_generation = 0

    @classmethod
    def from_crawler(cls, crawler):
        s = crawler.settings
        proxy_url = s.get("SGPROXY_URL")
        if not proxy_url:
            raise NotConfigured("SGPROXY_URL is not set")
        rotate_url = s.get("SGPROXY_ROTATE_URL") or ""
        proxy = MobileProxy(
            proxy_url,
            rotate_url,
            min_interval=s.getfloat("SGPROXY_MIN_INTERVAL", 0),
            ip_check_url=s.get("SGPROXY_IP_CHECK_URL", "https://api.ipify.org"),
        )
        return cls(
            proxy,
            can_rotate=bool(rotate_url),
            rotate_on={int(x) for x in s.getlist("SGPROXY_ROTATE_ON", [403, 429])},
            max_retries=s.getint("SGPROXY_MAX_RETRIES", 2),
            rotate_every=s.getint("SGPROXY_ROTATE_EVERY", 0),
            wait_for_new_ip=s.getbool("SGPROXY_WAIT_FOR_NEW_IP", True),
            rotate_timeout=s.getfloat("SGPROXY_ROTATE_TIMEOUT", 90),
            stats=crawler.stats,
        )

    def _inc(self, key: str) -> None:
        if self.stats is not None:
            self.stats.inc_value(f"sgproxy/{key}")

    async def _rotate(self, seen_generation: int, reason: str) -> None:
        """Rotate once, unless another request already did since ``seen_generation``."""
        await maybe_deferred_to_future(self._lock.acquire())
        try:
            if self.generation != seen_generation:
                return
            logger.info("Rotating mobile proxy IP (%s)", reason)
            try:
                result = await maybe_deferred_to_future(
                    self._run_in_thread(
                        self.proxy.rotate,
                        wait_for_new_ip=self.wait_for_new_ip,
                        timeout=self.rotate_timeout,
                    )
                )
            except RotationError as exc:
                self._inc("rotation_errors")
                logger.warning("Rotation failed: %s (HTTP %s)", exc, exc.status_code)
                return
            self._inc("rotations")
            if self.wait_for_new_ip and not result.changed:
                self._inc("rotations_same_ip")
                logger.warning("Rotation kept the same IP (%s)", result.old_ip)
            else:
                logger.info("New IP: %s -> %s", result.old_ip, result.new_ip)
            self.generation += 1
            self._sent_this_generation = 0
        finally:
            self._lock.release()

    async def process_request(self, request: Request, spider=None) -> None:
        if request.meta.get("dont_proxy"):
            return None
        if "proxy" in request.meta and request.meta["proxy"] != self.proxy.proxy_url:
            return None  # the spider picked its own proxy (or None) for this one
        if self.can_rotate and self.rotate_every and self._sent_this_generation >= self.rotate_every:
            await self._rotate(self.generation, f"every {self.rotate_every} requests")
        request.meta["proxy"] = self.proxy.proxy_url
        request.meta[META_GENERATION] = self.generation
        self._sent_this_generation += 1
        return None

    async def process_response(self, request: Request, response: Response, spider=None):
        if (
            not self.can_rotate
            or response.status not in self.rotate_on
            or META_GENERATION not in request.meta
        ):
            return response
        retries = request.meta.get(META_RETRIES, 0)
        if retries >= self.max_retries:
            self._inc("gave_up")
            return response
        await self._rotate(request.meta[META_GENERATION], f"HTTP {response.status} from {request.url}")
        self._inc("retries")
        retry = request.replace(dont_filter=True)
        retry.meta[META_RETRIES] = retries + 1
        # Let process_request stamp it with the current proxy and generation.
        retry.meta.pop("proxy", None)
        retry.meta.pop(META_GENERATION, None)
        return retry
