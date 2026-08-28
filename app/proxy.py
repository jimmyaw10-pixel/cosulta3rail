import logging
import time
from itertools import cycle
from typing import Any, Optional
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger("proxy")

_pool: list[str] = []
_cycle = None
_blocked: dict[str, float] = {}


def _init_pool() -> None:
    global _pool, _cycle
    raw = (settings.proxy_list or "").strip()
    if not raw:
        _pool = []
        _cycle = None
        return
    _pool = [p.strip() for p in raw.split(",") if p.strip()]
    _cycle = cycle(_pool) if _pool else None
    logger.info("Pool de proxies cargado: %s entradas", len(_pool))


def proxies_enabled() -> bool:
    if not _pool and settings.proxy_list:
        _init_pool()
    return bool(_pool)


def parse_proxy(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError(f"Proxy inválido: {url}")
    port = parsed.port or (1080 if parsed.scheme == "socks5" else 8080)
    out: dict[str, Any] = {"server": f"{parsed.scheme}://{parsed.hostname}:{port}"}
    if parsed.username:
        out["username"] = parsed.username
    if parsed.password:
        out["password"] = parsed.password
    return out


def mark_proxy_blocked(url: str) -> None:
    _blocked[url] = time.time() + settings.ibal_limit_cooldown_seconds
    logger.warning("Proxy en pausa %s...", url.split("@")[-1][:40])


def next_proxy() -> Optional[tuple[dict[str, Any], str]]:
    """Devuelve (config Playwright, url original del pool) o None."""
    if not settings.proxy_rotate:
        if settings.proxy_list and not _pool:
            _init_pool()
        if _pool:
            url = _pool[0]
            if time.time() < _blocked.get(url, 0):
                return None
            return parse_proxy(url), url
        return None

    if not _pool:
        _init_pool()
    if not _cycle:
        return None

    now = time.time()
    for _ in range(len(_pool)):
        url = next(_cycle)
        if now >= _blocked.get(url, 0):
            return parse_proxy(url), url
    logger.warning("Todos los proxies están en pausa por límite IBAL")
    return None
