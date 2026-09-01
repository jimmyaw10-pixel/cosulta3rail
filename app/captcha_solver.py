import asyncio
import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger("captcha")


class CaptchaSolverError(Exception):
    pass


def solver_disponible() -> bool:
    mode = (settings.captcha_solver or "").strip().lower()
    return bool(settings.captcha_api_key and mode in {"2captcha", "capsolver", "auto"})


def _resolver_proveedor(intento: int) -> Optional[str]:
    mode = (settings.captcha_solver or "").strip().lower()
    if mode in {"2captcha", "capsolver"} and settings.captcha_api_key:
        return mode
    if mode == "auto" and settings.captcha_api_key and intento >= 1:
        return settings.captcha_fallback or "capsolver"
    return None


def _task_types(proxy_url: Optional[str] = None) -> list[str]:
    primary = (settings.captcha_task_type or "ReCaptchaV3M1TaskProxyLess").strip()
    fallbacks = [
        "ReCaptchaV3M1TaskProxyLess",
        "ReCaptchaV3TaskProxyLess",
        "ReCaptchaV3EnterpriseTaskProxyLess",
    ]
    if proxy_url:
        fallbacks.extend(
            [
                "ReCaptchaV3Task",
                "ReCaptchaV3EnterpriseTask",
            ]
        )
    ordered = [primary] + [t for t in fallbacks if t != primary]
    seen: set[str] = set()
    out: list[str] = []
    for task_type in ordered:
        if task_type not in seen:
            seen.add(task_type)
            out.append(task_type)
    return out


def _build_capsolver_recaptcha_task(
    page_url: str,
    site_key: str,
    action: str,
    task_type: str,
    proxy_url: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "type": task_type,
        "websiteURL": page_url,
        "websiteKey": site_key,
    }
    if action:
        task["pageAction"] = action
    proxyless = "ProxyLess" in task_type
    if not proxyless and proxy_url:
        from app.proxy import proxy_sticky_url, proxy_to_capsolver_format

        sticky = proxy_sticky_url(proxy_url, "ibalcf")
        task["proxy"] = proxy_to_capsolver_format(sticky)
    if user_agent and not proxyless:
        task["userAgent"] = user_agent
    return task


async def _capsolver_create_task(client: httpx.AsyncClient, task: dict[str, Any]) -> str:
    res = await client.post(
        "https://api.capsolver.com/createTask",
        json={"clientKey": settings.captcha_api_key, "task": task},
    )
    try:
        data = res.json()
    except Exception as exc:
        raise CaptchaSolverError(
            f"CapSolver respuesta inválida (HTTP {res.status_code}): {res.text[:300]}"
        ) from exc
    if data.get("errorId"):
        code = data.get("errorCode") or "ERROR"
        desc = data.get("errorDescription") or data
        raise CaptchaSolverError(f"CapSolver {code}: {desc}")
    if res.status_code >= 400:
        raise CaptchaSolverError(f"CapSolver HTTP {res.status_code}: {data}")
    task_id = data.get("taskId")
    if not task_id:
        raise CaptchaSolverError(f"CapSolver sin taskId: {data}")
    return str(task_id)


async def _poll_2captcha(client: httpx.AsyncClient, task_id: str) -> str:
    for _ in range(40):
        await asyncio.sleep(5)
        res = await client.get(
            "https://2captcha.com/res.php",
            params={
                "key": settings.captcha_api_key,
                "action": "get",
                "id": task_id,
                "json": 1,
            },
        )
        res.raise_for_status()
        data = res.json()
        if data.get("status") == 1:
            token = data.get("request")
            if token:
                return str(token)
            break
        if data.get("request") != "CAPCHA_NOT_READY":
            raise CaptchaSolverError(f"2captcha: {data.get('request', data)}")
    raise CaptchaSolverError("2captcha no devolvió token a tiempo")


async def _solve_2captcha(
    page_url: str,
    site_key: str,
    action: str,
    min_score: float,
) -> str:
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        create = await client.get(
            "https://2captcha.com/in.php",
            params={
                "key": settings.captcha_api_key,
                "method": "userrecaptcha",
                "version": "v3",
                "googlekey": site_key,
                "pageurl": page_url,
                "action": action,
                "min_score": min_score,
                "json": 1,
            },
        )
        create.raise_for_status()
        payload = create.json()
        if payload.get("status") != 1:
            raise CaptchaSolverError(f"2captcha create: {payload.get('request', payload)}")
        task_id = str(payload["request"])
        logger.info("2captcha tarea %s creada (score %.1f)", task_id, min_score)
        return await _poll_2captcha(client, task_id)


async def _poll_capsolver_solution(client: httpx.AsyncClient, task_id: str) -> dict:
    for _ in range(40):
        await asyncio.sleep(2)
        res = await client.post(
            "https://api.capsolver.com/getTaskResult",
            json={"clientKey": settings.captcha_api_key, "taskId": task_id},
        )
        res.raise_for_status()
        data = res.json()
        if data.get("errorId"):
            raise CaptchaSolverError(f"CapSolver: {data.get('errorDescription', data)}")
        status = data.get("status")
        if status == "ready":
            solution = data.get("solution") or {}
            if solution:
                return solution
            break
        if status == "failed":
            raise CaptchaSolverError(f"CapSolver falló: {data}")
    raise CaptchaSolverError("CapSolver no devolvió solución a tiempo")


async def _poll_capsolver(client: httpx.AsyncClient, task_id: str) -> str:
    solution = await _poll_capsolver_solution(client, task_id)
    token = solution.get("gRecaptchaResponse") or solution.get("token")
    if token:
        score = solution.get("score")
        logger.info("CapSolver token listo (score reportado: %s)", score)
        return str(token)
    raise CaptchaSolverError("CapSolver no devolvió token reCAPTCHA")


async def solve_cloudflare(proxy_url: str) -> dict:
    from app.proxy import proxy_sticky_url, proxy_to_capsolver_format

    sticky = proxy_sticky_url(proxy_url, "ibalcf")
    proxy = proxy_to_capsolver_format(sticky)
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
        task_id = await _capsolver_create_task(
            client,
            {
                "type": "AntiCloudflareTask",
                "websiteURL": settings.ibal_base_url,
                "proxy": proxy,
            },
        )
        logger.info("CapSolver Cloudflare tarea %s", task_id)
        return await _poll_capsolver_solution(client, task_id)


async def _solve_capsolver_once(
    page_url: str,
    site_key: str,
    action: str,
    task_type: str,
    proxy_url: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> str:
    task = _build_capsolver_recaptcha_task(
        page_url,
        site_key,
        action,
        task_type,
        proxy_url=proxy_url,
        user_agent=user_agent,
    )
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        task_id = await _capsolver_create_task(client, task)
        logger.info("CapSolver tarea %s (%s)", task_id, task_type)
        return await _poll_capsolver(client, task_id)


async def _solve_capsolver(
    page_url: str,
    site_key: str,
    action: str,
    proxy_url: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> str:
    last_error: Optional[Exception] = None
    for task_type in _task_types(proxy_url):
        try:
            return await _solve_capsolver_once(
                page_url,
                site_key,
                action,
                task_type,
                proxy_url=proxy_url,
                user_agent=user_agent,
            )
        except CaptchaSolverError as exc:
            last_error = exc
            logger.warning("CapSolver %s falló: %s", task_type, exc)
    if last_error:
        raise last_error
    raise CaptchaSolverError("CapSolver no pudo crear ninguna tarea reCAPTCHA")


async def solve_recaptcha_v3(
    provider: str,
    page_url: Optional[str] = None,
    site_key: Optional[str] = None,
    action: Optional[str] = None,
    min_score: Optional[float] = None,
    user_agent: Optional[str] = None,
    proxy_url: Optional[str] = None,
) -> str:
    page_url = page_url or settings.ibal_base_url
    site_key = site_key or settings.recaptcha_site_key
    action = action or settings.recaptcha_action
    provider = provider.strip().lower()
    if provider == "2captcha":
        score = settings.captcha_min_score if min_score is None else min_score
        return await _solve_2captcha(page_url, site_key, action, score)
    if provider == "capsolver":
        return await _solve_capsolver(
            page_url,
            site_key,
            action,
            proxy_url=proxy_url,
            user_agent=user_agent,
        )
    raise CaptchaSolverError(f"Proveedor de captcha desconocido: {provider}")
