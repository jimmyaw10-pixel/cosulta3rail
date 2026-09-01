import logging
import time
from itertools import cycle
from typing import Any, Optional
from urllib.parse import quote, unquote

import httpx

from app.config import settings

logger = logging.getLogger("proxy")

_pool: list[str] = []
_cycle = None
_blocked: dict[str, float] = {}
_gov_blocked: set[str] = set()

DATAIMPULSE_GOV_MSG = (
    "El proxy DataImpulse bloquea sitios .gov (ibal.gov.co). "
    "DataImpulse no permite portales gubernamentales por defecto. "
    "Opciones: pedir desbloqueo en support@dataimpulse.com (requiere KYC), "
    "usar otro proxy residencial de Colombia, o dejar PROXY_LIST vacío."
)


def is_dataimpulse_site_blocked(text: str) -> bool:
    upper = (text or "").upper()
    return any(
        marker in upper
        for marker in (
            "SITE_PERMANENTLY_BLOCKED",
            "HOST_BLOCKED",
            "403 SITE_PERMANENTLY",
        )
    )


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


def parse_proxy_parts(url: str) -> dict[str, Any]:
    """Parsea proxy DataImpulse sin depender de urlparse (username puede llevar ;)."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("Proxy vacío")
    if "://" not in raw:
        raw = f"http://{raw}"
    scheme, rest = raw.split("://", 1)
    scheme = scheme.lower()
    if "@" not in rest:
        raise ValueError(f"Proxy sin credenciales: {url}")
    auth, hostport = rest.rsplit("@", 1)
    username, password = auth.rsplit(":", 1)
    username = unquote(username)
    password = unquote(password)
    if ":" in hostport:
        hostname, port_str = hostport.rsplit(":", 1)
        port = int(port_str)
    else:
        hostname = hostport
        port = 823 if scheme in {"http", "https"} else 1080
    return {
        "scheme": scheme,
        "username": username,
        "password": password,
        "hostname": hostname,
        "port": port,
    }


def with_sticky_session(username: str, session_id: str) -> str:
    if not session_id or f"sessid.{session_id}" in username:
        return username
    return f"{username};sessid.{session_id}"


def httpx_proxy_url(url: str, sticky_session: str = "") -> str:
    parts = parse_proxy_parts(url)
    username = parts["username"]
    if sticky_session:
        username = with_sticky_session(username, sticky_session)
    user = quote(username, safe=";._-~")
    password = quote(parts["password"], safe="")
    return f"http://{user}:{password}@{parts['hostname']}:{parts['port']}"


def playwright_proxy_config(
    url: str,
    sticky_session: str = "",
    protocol: str = "http",
) -> dict[str, Any]:
    parts = parse_proxy_parts(url)
    username = parts["username"]
    if sticky_session:
        username = with_sticky_session(username, sticky_session)
    server_scheme = protocol if protocol in {"http", "socks5"} else "http"
    return {
        "server": f"{server_scheme}://{parts['hostname']}:{parts['port']}",
        "username": username,
        "password": parts["password"],
    }


def proxy_to_capsolver_format(url: str, sticky_session: str = "") -> str:
    parts = parse_proxy_parts(url)
    username = parts["username"]
    if sticky_session:
        username = with_sticky_session(username, sticky_session)
    return f"{parts['hostname']}:{parts['port']}:{username}:{parts['password']}"


def parse_proxy(url: str, sticky_session: str = "") -> dict[str, Any]:
    return playwright_proxy_config(url, sticky_session=sticky_session)


async def verify_proxy(url: str, sticky_session: str = "") -> tuple[bool, str]:
    proxy = httpx_proxy_url(url, sticky_session)
    try:
        async with httpx.AsyncClient(
            proxy=proxy,
            timeout=httpx.Timeout(25.0),
            follow_redirects=True,
        ) as client:
            res = await client.get("https://api.ipify.org/")
            res.raise_for_status()
            ip = res.text.strip()
            logger.info("Proxy verificado OK (IP %s)", ip)
            return True, ip
    except Exception as exc:
        logger.warning("Proxy no responde (%s): %s", sticky_session or "rotativo", exc)
        return False, str(exc)


def mark_proxy_blocked(url: str) -> None:
    _blocked[url] = time.time() + settings.ibal_limit_cooldown_seconds
    logger.warning("Proxy en pausa %s...", url.split("@")[-1][:40])


def mark_proxy_gov_blocked(url: str) -> None:
    _gov_blocked.add(url)
    mark_proxy_blocked(url)
    logger.warning("Proxy marcado como bloqueado para .gov: %s", url.split("@")[-1][:40])


def next_proxy() -> Optional[tuple[str, str]]:
    """Devuelve (url original del pool, url original) — config se arma aparte."""
    if not settings.proxy_rotate:
        if settings.proxy_list and not _pool:
            _init_pool()
        if _pool:
            url = _pool[0]
            if url in _gov_blocked:
                return None
            if time.time() < _blocked.get(url, 0):
                return None
            return url, url
        return None

    if not _pool:
        _init_pool()
    if not _cycle:
        return None

    now = time.time()
    for _ in range(len(_pool)):
        url = next(_cycle)
        if url in _gov_blocked:
            continue
        if now >= _blocked.get(url, 0):
            return url, url
    logger.warning("Todos los proxies están en pausa por límite IBAL")
    return None


# Compatibilidad con código que usaba proxy_sticky_url
def proxy_sticky_url(url: str, session_id: str = "ibalcf") -> str:
    parts = parse_proxy_parts(url)
    username = with_sticky_session(parts["username"], session_id)
    return (
        f"{parts['scheme']}://{username}:{parts['password']}"
        f"@{parts['hostname']}:{parts['port']}"
    )
