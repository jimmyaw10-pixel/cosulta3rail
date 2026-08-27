import logging
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.models import ConsultaResponse
from app.parser import detect_block_reason, parse_factura_html

logger = logging.getLogger("ibal")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

MATRICULA_RE = re.compile(r"^\d{3,12}$")

_playwright = None
_browser = None


class ConsultaError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def validar_matricula(matricula: str) -> str:
    value = (matricula or "").strip()
    if not MATRICULA_RE.match(value):
        raise ConsultaError(
            "La matrícula debe contener solo dígitos (3 a 12 caracteres).",
            status_code=400,
        )
    return value


def _headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
        "Origin": "https://ibal.gov.co",
        "Referer": settings.ibal_base_url,
    }


def _extract_csrf(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "lxml")
    field = soup.find("input", {"name": "csrf_test_name"})
    if field and field.get("value"):
        return str(field["value"])
    return None


def _build_response(
    matricula: str,
    html: str,
    motor: str,
) -> Optional[ConsultaResponse]:
    factura, sin_resultados = parse_factura_html(html)
    if sin_resultados:
        return ConsultaResponse(
            ok=True,
            matricula_consultada=matricula,
            encontrada=False,
            mensaje=sin_resultados,
            factura=None,
            motor=motor,
        )
    if factura:
        return ConsultaResponse(
            ok=True,
            matricula_consultada=matricula,
            encontrada=True,
            mensaje="Factura encontrada",
            factura=factura,
            motor=motor,
        )
    return None


async def consultar_http(matricula: str) -> ConsultaResponse:
    timeout = httpx.Timeout(settings.ibal_timeout_seconds)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=_headers(),
    ) as client:
        landing = await client.get(settings.ibal_base_url)
        landing.raise_for_status()
        csrf = _extract_csrf(landing.text)
        if not csrf:
            raise ConsultaError("No se pudo obtener el token CSRF de IBAL.")

        payload = {
            "csrf_test_name": csrf,
            "g-recaptcha-response": "",
            "matricula_cliente": matricula,
        }
        result = await client.post(settings.ibal_base_url, data=payload)
        result.raise_for_status()
        html = result.text

        parsed = _build_response(matricula, html, "http")
        if parsed:
            return parsed

        reason = detect_block_reason(html)
        raise ConsultaError(
            "IBAL no devolvió datos de factura por HTTP "
            f"({reason or 'posible reCAPTCHA o sesión inválida'})."
        )


async def start_browser() -> None:
    global _playwright, _browser
    if _browser is not None:
        return
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise ConsultaError(
            "Playwright no está instalado. Use IBAL_ENGINE=http o instale playwright.",
            status_code=500,
        ) from exc

    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    logger.info("Navegador Chromium iniciado")


async def stop_browser() -> None:
    global _playwright, _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None


async def consultar_browser(matricula: str) -> ConsultaResponse:
    if _browser is None:
        await start_browser()

    context = await _browser.new_context(
        user_agent=USER_AGENT,
        locale="es-CO",
        viewport={"width": 1280, "height": 900},
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    page = await context.new_page()
    try:
        await page.goto(settings.ibal_base_url, wait_until="domcontentloaded")
        await page.wait_for_selector('input[name="matricula_cliente"]', timeout=20000)
        await page.wait_for_function(
            "() => window.grecaptcha && typeof window.grecaptcha.execute === 'function'",
            timeout=20000,
        )

        desktop = page.locator("#form_consulta_desktop input[name='matricula_cliente']")
        use_desktop = await desktop.count() and await desktop.first.is_visible()
        if use_desktop:
            await desktop.first.fill(matricula)
            submit = page.locator("#busca_desktop")
        else:
            await page.locator(
                "#form_consulta_mobile input[name='matricula_cliente']"
            ).first.fill(matricula)
            submit = page.locator("#busca_mobile")

        async with page.expect_navigation(timeout=45000, wait_until="domcontentloaded"):
            await submit.click()

        try:
            await page.wait_for_function(
                """() => {
                  const t = (document.body && document.body.innerText) || '';
                  return t.includes('FECHA DE SUSPENSI') ||
                         t.includes('NÚMERO DE FACTURA') ||
                         t.includes('NUMERO DE FACTURA') ||
                         t.includes('No se encuetran') ||
                         t.includes('No se encuentran');
                }""",
                timeout=20000,
            )
        except Exception:
            await page.wait_for_timeout(2000)

        html = await page.content()
        parsed = _build_response(matricula, html, "browser")
        if parsed:
            return parsed
        raise ConsultaError(
            "La consulta en el portal IBAL no devolvió una factura reconocible."
        )
    finally:
        await context.close()


async def consultar_factura(matricula: str) -> ConsultaResponse:
    matricula = validar_matricula(matricula)
    engine = (settings.ibal_engine or "auto").strip().lower()

    if engine == "http":
        return await consultar_http(matricula)
    if engine == "browser":
        return await consultar_browser(matricula)

    try:
        return await consultar_http(matricula)
    except ConsultaError as exc:
        logger.warning("HTTP falló (%s), usando navegador", exc)
        return await consultar_browser(matricula)
    except httpx.HTTPError as exc:
        logger.warning("Error HTTP de IBAL (%s), usando navegador", exc)
        return await consultar_browser(matricula)
