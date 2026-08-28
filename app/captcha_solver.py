import asyncio
import logging
from typing import Optional

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
        logger.info("2captcha tarea %s creada", task_id)
        return await _poll_2captcha(client, task_id)


async def _poll_capsolver(client: httpx.AsyncClient, task_id: str) -> str:
    for _ in range(40):
        await asyncio.sleep(3)
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
            token = (data.get("solution") or {}).get("gRecaptchaResponse")
            if token:
                return str(token)
            break
        if status == "failed":
            raise CaptchaSolverError(f"CapSolver falló: {data}")
    raise CaptchaSolverError("CapSolver no devolvió token a tiempo")


async def _solve_capsolver(
    page_url: str,
    site_key: str,
    action: str,
    min_score: float,
) -> str:
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        create = await client.post(
            "https://api.capsolver.com/createTask",
            json={
                "clientKey": settings.captcha_api_key,
                "task": {
                    "type": "ReCaptchaV3TaskProxyLess",
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                    "pageAction": action,
                    "minScore": min_score,
                },
            },
        )
        create.raise_for_status()
        data = create.json()
        if data.get("errorId"):
            raise CaptchaSolverError(f"CapSolver create: {data.get('errorDescription', data)}")
        task_id = data.get("taskId")
        if not task_id:
            raise CaptchaSolverError(f"CapSolver sin taskId: {data}")
        logger.info("CapSolver tarea %s creada", task_id)
        return await _poll_capsolver(client, task_id)


async def solve_recaptcha_v3(
    provider: str,
    page_url: Optional[str] = None,
    site_key: Optional[str] = None,
    action: Optional[str] = None,
    min_score: Optional[float] = None,
) -> str:
    page_url = page_url or settings.ibal_base_url
    site_key = site_key or settings.recaptcha_site_key
    action = action or settings.recaptcha_action
    min_score = settings.captcha_min_score if min_score is None else min_score
    provider = provider.strip().lower()
    if provider == "2captcha":
        return await _solve_2captcha(page_url, site_key, action, min_score)
    if provider == "capsolver":
        return await _solve_capsolver(page_url, site_key, action, min_score)
    raise CaptchaSolverError(f"Proveedor de captcha desconocido: {provider}")
