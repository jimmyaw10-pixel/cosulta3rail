import logging
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.models import ConsultaResponse
from app.parser import (
    compact_text,
    describe_empty_html,
    detect_antibot_page,
    ibal_block_message,
    is_landing_page,
    pagina_aun_cargando,
    parse_factura_html,
)
from app.captcha_solver import (
    CaptchaSolverError,
    _resolver_proveedor,
    solve_cloudflare,
    solve_recaptcha_v3,
    solver_disponible,
)
from app.parser import es_challenge_cloudflare
from app.proxy import (
    httpx_proxy_url,
    mark_proxy_blocked,
    next_proxy,
    playwright_proxy_config,
    proxies_enabled,
    verify_proxy,
)

logger = logging.getLogger("ibal")

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'languages', {get: () => ['es-CO', 'es', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
  parameters.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : originalQuery(parameters)
);
"""


def _empty_consulta(matricula: str, html: str, motor: str) -> ConsultaResponse:
    return ConsultaResponse(
        ok=False,
        matricula_consultada=matricula,
        encontrada=False,
        mensaje=describe_empty_html(html),
        factura=None,
        motor=motor,
        debug_texto=compact_text(html),
    )

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MATRICULA_RE = re.compile(r"^\d{3,12}$")

_playwright = None
_browser = None
_current_proxy_url: Optional[str] = None


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


def _marcar_limite_ibal() -> None:
    logger.warning("IBAL respondió límite de consultas en esta petición")


async def _esperar_cupo_ibal() -> None:
    return


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
    bloqueo = ibal_block_message(html)
    if bloqueo:
        if _current_proxy_url:
            mark_proxy_blocked(_current_proxy_url)
        else:
            _marcar_limite_ibal()
        raise ConsultaError(bloqueo, status_code=429)
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
        parsed = _build_response(matricula, result.text, "http")
        if parsed:
            return parsed
        return _empty_consulta(matricula, result.text, "http")


def _cf_cookies_dict(solution: dict) -> dict[str, str]:
    cookies: dict[str, str] = {}
    raw = solution.get("cookies") or []
    if isinstance(raw, dict):
        for name, value in raw.items():
            cookies[str(name)] = str(value)
    else:
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                cookies[str(item["name"])] = str(item.get("value", ""))
    token = solution.get("token")
    if token and "cf_clearance" not in cookies:
        cookies["cf_clearance"] = str(token)
    return cookies


async def consultar_httpx_proxy(matricula: str) -> ConsultaResponse:
    """Consulta IBAL vía httpx + proxy (evita ERR_TUNNEL de Playwright en Railway)."""
    global _current_proxy_url
    picked = next_proxy()
    if not picked:
        if proxies_enabled():
            raise ConsultaError(
                "Todos los proxies están en pausa. Espera unos minutos o agrega más IPs.",
                status_code=429,
            )
        raise ConsultaError("No hay proxy configurado.", status_code=502)

    _current_proxy_url, _ = picked
    sticky = "ibalcf"
    ok, detail = await verify_proxy(_current_proxy_url, sticky)
    if not ok:
        ok, detail = await verify_proxy(_current_proxy_url, "")
        sticky = ""
    if not ok:
        raise ConsultaError(
            f"No se pudo conectar al proxy DataImpulse desde el servidor: {detail}",
            status_code=502,
        )

    user_agent = USER_AGENT
    cookies: dict[str, str] = {}
    if (
        settings.ibal_cloudflare_bypass
        and solver_disponible()
        and (settings.captcha_solver or "").strip().lower() == "capsolver"
    ):
        try:
            cf_solution = await solve_cloudflare(_current_proxy_url)
            if cf_solution.get("userAgent"):
                user_agent = str(cf_solution["userAgent"])
            cookies = _cf_cookies_dict(cf_solution)
            logger.info("Cloudflare cf_clearance obtenido (%s cookies)", len(cookies))
        except (CaptchaSolverError, httpx.HTTPError) as exc:
            logger.warning("Bypass Cloudflare CapSolver falló: %s", exc)

    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
        "Origin": "https://ibal.gov.co",
        "Referer": settings.ibal_base_url,
    }
    proxy = httpx_proxy_url(_current_proxy_url, sticky)
    timeout = httpx.Timeout(settings.ibal_timeout_seconds)

    async with httpx.AsyncClient(
        proxy=proxy,
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
        cookies=cookies,
    ) as client:
        landing = await client.get(settings.ibal_base_url)
        landing.raise_for_status()
        html = landing.text

        if es_challenge_cloudflare(html) and not cookies:
            raise ConsultaError(
                "Cloudflare bloquea el acceso. CapSolver no pudo obtener cf_clearance.",
                status_code=502,
            )

        csrf = _extract_csrf(html)
        if not csrf:
            bloqueo = ibal_block_message(html)
            if bloqueo:
                mark_proxy_blocked(_current_proxy_url)
                raise ConsultaError(bloqueo, status_code=429)
            antibot = detect_antibot_page(html)
            raise ConsultaError(
                antibot or "No se pudo obtener el token CSRF de IBAL.",
                status_code=502,
            )

        proveedor = _resolver_proveedor(0)
        if not proveedor:
            raise ConsultaError(
                "Se requiere CapSolver para reCAPTCHA v3 con proxy.",
                status_code=502,
            )
        try:
            token = await solve_recaptcha_v3(
                proveedor,
                user_agent=user_agent,
                proxy_url=_current_proxy_url,
            )
        except CaptchaSolverError as exc:
            raise ConsultaError(
                f"No se pudo resolver reCAPTCHA ({proveedor}): {exc}",
                status_code=502,
            ) from exc

        result = await client.post(
            settings.ibal_base_url,
            data={
                "csrf_test_name": csrf,
                "g-recaptcha-response": token,
                "matricula_cliente": matricula,
            },
        )
        result.raise_for_status()
        parsed = _build_response(matricula, result.text, "httpx-proxy")
        if parsed:
            return parsed
        return _empty_consulta(matricula, result.text, "httpx-proxy")


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
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
        ],
        ignore_default_args=["--enable-automation"],
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


async def _recaptcha_token(page, site_key: str) -> str:
    token = await page.evaluate(
        """async (siteKey) => {
          if (!window.grecaptcha || !window.grecaptcha.execute) {
            throw new Error("reCAPTCHA no cargó en el portal IBAL");
          }
          await new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error("grecaptcha.ready timeout")), 45000);
            window.grecaptcha.ready(() => {
              clearTimeout(timer);
              resolve();
            });
          });
          return await window.grecaptcha.execute(siteKey, {action: "consulta_pago"});
        }""",
        site_key,
    )
    if not token or not isinstance(token, str):
        raise ConsultaError("IBAL no emitió token de reCAPTCHA. Reintenta en unos segundos.")
    return token


async def _simular_usuario(page) -> None:
    await page.mouse.move(220, 180, steps=12)
    await page.hover("#form_consulta_desktop")
    await page.mouse.move(480, 320, steps=10)
    await page.wait_for_timeout(settings.ibal_recaptcha_warmup_ms)


async def _cookies_cloudflare(solution: dict) -> list[dict]:
    out: list[dict] = []
    cookies_raw = solution.get("cookies") or []
    if isinstance(cookies_raw, dict):
        for name, value in cookies_raw.items():
            out.append(
                {
                    "name": str(name),
                    "value": str(value),
                    "domain": ".ibal.gov.co",
                    "path": "/",
                }
            )
    else:
        for item in cookies_raw:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            out.append(
                {
                    "name": str(item["name"]),
                    "value": str(item.get("value", "")),
                    "domain": str(item.get("domain") or ".ibal.gov.co"),
                    "path": str(item.get("path") or "/"),
                }
            )
    token = solution.get("token")
    if token and not any(c["name"] == "cf_clearance" for c in out):
        out.append(
            {
                "name": "cf_clearance",
                "value": str(token),
                "domain": ".ibal.gov.co",
                "path": "/",
            }
        )
    return out


async def _cargar_portal(page) -> None:
    ultimo_html = ""
    for intento in range(3):
        if intento > 0:
            logger.info("Reintentando cargar IBAL (%s/3)", intento + 1)
            await page.goto(settings.ibal_base_url, wait_until="load", timeout=60000)
        else:
            await page.goto(settings.ibal_base_url, wait_until="load", timeout=60000)

        for _ in range(25):
            if await page.locator("#form_consulta_desktop").count() > 0:
                break
            await page.wait_for_timeout(2000)
        else:
            ultimo_html = await page.content()
            if intento < 2:
                continue
            bloqueo = ibal_block_message(ultimo_html)
            if bloqueo:
                if _current_proxy_url:
                    mark_proxy_blocked(_current_proxy_url)
                else:
                    _marcar_limite_ibal()
                raise ConsultaError(bloqueo, status_code=429)
            antibot = detect_antibot_page(ultimo_html)
            if es_challenge_cloudflare(ultimo_html) and not proxies_enabled():
                raise ConsultaError(
                    "Cloudflare bloquea la IP de Railway. Agrega PROXY_LIST con proxy "
                    "residencial de Colombia (ej. CapSolver/DataImpulse) y redeploy.",
                    status_code=502,
                )
            raise ConsultaError(
                antibot or "No se pudo cargar el formulario IBAL tras varios intentos.",
                status_code=502,
            )
        break

    await page.wait_for_function(
        "() => window.grecaptcha && typeof window.grecaptcha.execute === 'function'",
        timeout=45000,
    )
    await _simular_usuario(page)


def _html_tiene_resultado(html: str) -> bool:
    if ibal_block_message(html):
        return False
    factura, sin = parse_factura_html(html)
    return factura is not None or sin is not None


async def _extraer_recaptcha_action(page) -> str:
    action = await page.evaluate(
        """() => {
          const html = document.documentElement.innerHTML;
          const m = html.match(
            /grecaptcha\\.execute\\([^,]+,\\s*\\{[^}]*action\\s*:\\s*['"]([^'"]+)['"]/i
          );
          return m ? m[1] : null;
        }"""
    )
    if action:
        logger.info("pageAction detectada en IBAL: %s", action)
        return str(action)
    return settings.recaptcha_action


async def _enganchar_token_recaptcha(page, token: str) -> None:
    await page.evaluate(
        """(token) => {
          const apply = (gr) => {
            if (!gr) return;
            gr.execute = async () => token;
            if (gr.enterprise) gr.enterprise.execute = async () => token;
          };
          apply(window.grecaptcha);
          const started = Date.now();
          const timer = setInterval(() => {
            apply(window.grecaptcha);
            if (Date.now() - started > 8000) clearInterval(timer);
          }, 150);
        }""",
        token,
    )


async def _enviar_con_clic_portal(page, matricula: str, token: str) -> str:
    await page.fill("#form_consulta_desktop input[name='matricula_cliente']", matricula)
    await _inyectar_token(page, token)
    await _enganchar_token_recaptcha(page, token)
    await page.wait_for_timeout(500)
    try:
        async with page.expect_navigation(timeout=60000, wait_until="domcontentloaded"):
            await page.click("#busca_desktop")
    except Exception as exc:
        logger.warning("Clic en busca_desktop sin navegación completa: %s", exc)
    await _esperar_resultado_ibal(page)
    html = await page.content()
    if _html_tiene_resultado(html):
        return html
    return await _post_con_token(page, matricula, token)


async def _inyectar_token(page, token: str) -> None:
    await page.evaluate(
        """(token) => {
          const form = document.getElementById('form_consulta_desktop');
          if (!form) throw new Error("form_consulta_desktop no encontrado");
          let field = form.querySelector('[name="g-recaptcha-response"]');
          if (!field) {
            field = document.createElement('textarea');
            field.name = 'g-recaptcha-response';
            field.style.display = 'none';
            form.appendChild(field);
          }
          field.value = token;
        }""",
        token,
    )


async def _esperar_resultado_ibal(page) -> None:
    try:
        await page.wait_for_function(
            """() => {
              const text = document.body?.innerText || '';
              if (/No se encue?ntran facturas/i.test(text)) return true;
              if (/L[ií]mite de consultas/i.test(text)) return true;
              if (/\\d{1,2}\\/\\d{1,2}\\/\\d{4}/.test(text) && /PAGO\\s+TOTAL/i.test(text)) {
                return /\\$\\s*[\\d.,]+/.test(text) || /NO PAGADA|PAGADA/i.test(text);
              }
              return false;
            }""",
            timeout=60000,
        )
    except Exception as exc:
        logger.warning("Timeout esperando tarjetas IBAL: %s", exc)


async def _enviar_consulta_token(page, matricula: str, token: str, origen: str) -> str:
    if origen in {"capsolver", "2captcha"}:
        return await _enviar_con_clic_portal(page, matricula, token)

    await page.fill("#form_consulta_desktop input[name='matricula_cliente']", matricula)
    await _inyectar_token(page, token)
    try:
        async with page.expect_navigation(timeout=60000, wait_until="domcontentloaded"):
            await page.evaluate(
                "document.getElementById('form_consulta_desktop').requestSubmit()"
            )
    except Exception as exc:
        logger.warning("requestSubmit no navegó (%s); probando POST con token", exc)
        return await _post_con_token(page, matricula, token)
    await _esperar_resultado_ibal(page)
    return await page.content()


async def _obtener_token(page, intento: int) -> tuple[str, str]:
    action = await _extraer_recaptcha_action(page)
    proveedor = _resolver_proveedor(intento)
    if proveedor:
        logger.info(
            "Resolviendo reCAPTCHA v3 con %s (intento %s, action=%s)",
            proveedor,
            intento + 1,
            action,
        )
        try:
            token = await solve_recaptcha_v3(
                proveedor,
                action=action,
                user_agent=USER_AGENT,
                proxy_url=_current_proxy_url,
            )
            return token, proveedor
        except CaptchaSolverError as exc:
            modo = (settings.captcha_solver or "").strip().lower()
            if modo in {"2captcha", "capsolver"}:
                raise ConsultaError(
                    f"No se pudo resolver reCAPTCHA ({proveedor}): {exc}",
                    status_code=502,
                ) from exc
            logger.warning("Solver %s falló, probando navegador: %s", proveedor, exc)

    token = await _recaptcha_token(page, settings.recaptcha_site_key)
    return token, "browser"


async def _intentar_consulta_token(page, matricula: str, intento: int) -> str:
    if intento > 0:
        logger.info("Reintento reCAPTCHA IBAL #%s", intento + 1)
        await page.goto(settings.ibal_base_url, wait_until="load", timeout=60000)
        await page.wait_for_selector("#form_consulta_desktop", timeout=45000)
        await page.wait_for_function(
            "() => window.grecaptcha && typeof window.grecaptcha.execute === 'function'",
            timeout=45000,
        )
        await _simular_usuario(page)
    else:
        await page.fill("#form_consulta_desktop input[name='matricula_cliente']", matricula)
        await page.wait_for_timeout(1500)

    await page.wait_for_timeout(800 + intento * 1500)
    token, origen = await _obtener_token(page, intento)
    logger.info("Token reCAPTCHA obtenido vía %s (%s chars)", origen, len(token))
    html = await _enviar_consulta_token(page, matricula, token, origen)
    if pagina_aun_cargando(html):
        await page.wait_for_timeout(6000)
        html = await page.content()
    return html


async def _post_con_token(page, matricula: str, token: str) -> str:
    csrf = await page.locator(
        "#form_consulta_desktop input[name='csrf_test_name']"
    ).input_value()
    response = await page.request.post(
        settings.ibal_base_url,
        form={
            "csrf_test_name": csrf or "",
            "g-recaptcha-response": token,
            "matricula_cliente": matricula,
        },
        headers={
            "Origin": "https://ibal.gov.co",
            "Referer": settings.ibal_base_url,
        },
        timeout=120000,
        max_redirects=5,
    )
    return await response.text()


async def consultar_browser(matricula: str) -> ConsultaResponse:
    global _current_proxy_url
    if _browser is None:
        await start_browser()

    proxy_cfg = None
    _current_proxy_url = None
    picked = next_proxy()
    if picked:
        _current_proxy_url, _ = picked
        sticky = "ibalcf"
        ok, _ = await verify_proxy(_current_proxy_url, sticky)
        if not ok:
            sticky = ""
        proxy_cfg = playwright_proxy_config(_current_proxy_url, sticky_session=sticky)
        logger.info(
            "Playwright proxy %s (sticky=%s)",
            proxy_cfg["server"],
            bool(sticky),
        )
    elif proxies_enabled():
        raise ConsultaError(
            "Todos los proxies están en pausa por límite IBAL. Espera unos minutos o agrega más IPs al pool.",
            status_code=429,
        )
    context_kwargs: dict = {
        "user_agent": USER_AGENT,
        "locale": "es-CO",
        "timezone_id": "America/Bogota",
        "geolocation": {"latitude": 4.4389, "longitude": -75.2322},
        "permissions": ["geolocation"],
        "viewport": {"width": 1366, "height": 900},
        "extra_http_headers": {"Accept-Language": "es-CO,es;q=0.9"},
    }
    if proxy_cfg:
        context_kwargs["proxy"] = proxy_cfg

    user_agent = USER_AGENT
    cf_cookies: list[dict] = []
    if (
        _current_proxy_url
        and settings.ibal_cloudflare_bypass
        and solver_disponible()
        and (settings.captcha_solver or "").strip().lower() == "capsolver"
    ):
        try:
            cf_solution = await solve_cloudflare(_current_proxy_url)
            if cf_solution.get("userAgent"):
                user_agent = str(cf_solution["userAgent"])
            cf_cookies = await _cookies_cloudflare(cf_solution)
            logger.info("Cloudflare cf_clearance obtenido (%s cookies)", len(cf_cookies))
        except (CaptchaSolverError, httpx.HTTPError) as exc:
            logger.warning("Bypass Cloudflare CapSolver falló: %s", exc)

    context_kwargs["user_agent"] = user_agent
    context = await _browser.new_context(**context_kwargs)
    if cf_cookies:
        await context.add_cookies(cf_cookies)
    await context.add_init_script(STEALTH_JS)
    page = await context.new_page()
    try:
        await _cargar_portal(page)
        html = ""
        max_intentos = max(1, settings.ibal_recaptcha_retries)
        for intento in range(max_intentos):
            html = await _intentar_consulta_token(page, matricula, intento)
            parsed = _build_response(matricula, html, "browser")
            if parsed:
                return parsed
            if _html_tiene_resultado(html):
                break
            if not is_landing_page(html) and not pagina_aun_cargando(html):
                break
            if intento + 1 < max_intentos:
                await page.wait_for_timeout(4000)

        return _empty_consulta(matricula, html, "browser")
    except ConsultaError:
        raise
    except Exception as exc:
        raise ConsultaError(f"No se pudo consultar el portal IBAL: {exc}") from exc
    finally:
        await context.close()


async def consultar_factura(matricula: str) -> ConsultaResponse:
    matricula = validar_matricula(matricula)
    await _esperar_cupo_ibal()
    engine = (settings.ibal_engine or "auto").strip().lower()

    if proxies_enabled():
        try:
            return await consultar_httpx_proxy(matricula)
        except ConsultaError as exc:
            logger.warning("httpx+proxy falló (%s), probando navegador", exc)
            if engine == "http":
                raise

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
