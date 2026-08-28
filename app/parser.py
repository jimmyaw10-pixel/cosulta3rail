import re
from typing import Optional

from bs4 import BeautifulSoup, NavigableString, Tag

from app.models import Factura

LABEL_FECHA = re.compile(
    r"FECHA\s+DE\s+(?:SUSPENSI[OÓ]N|VENCIMIENTO|CORTE)",
    re.I,
)
LABEL_PERIODO = re.compile(r"Periodo\s+de\s+facturaci[oó]n", re.I)
LABEL_MATRICULA = re.compile(r"N[ºO°.]?\s*MATR[IÍ]CULA", re.I)
LABEL_FACTURA = re.compile(r"N[ÚU]MERO\s+DE\s+FACTURA", re.I)
LABEL_NOMBRE = re.compile(r"NOMBRE\s+DEL\s+TITULAR", re.I)
LABEL_DIRECCION = re.compile(r"DIRECCI[OÓ]N\s+DEL\s+TITULAR", re.I)
LABEL_PAGO = re.compile(r"PAGO\s+TOTAL|TOTAL\s+A\s+PAGAR|VALOR\s+(?:A\s+PAGAR|TOTAL)", re.I)

SIN_FACTURA = re.compile(
    r"No se encue?ntran facturas pendientes|"
    r"no hay facturas pendientes|"
    r"no existen facturas|"
    r"matr[ií]cula no (?:existe|encontrada)|"
    r"sin facturas pendientes",
    re.I,
)
LIMITE_CONSULTAS = re.compile(
    r"L[ií]mite de consultas alcanzado|"
    r"l[ií]mite de consultas|"
    r"demasiadas consultas|"
    r"too many requests",
    re.I,
)

LABEL_SET = [
    LABEL_FECHA,
    LABEL_PERIODO,
    LABEL_MATRICULA,
    LABEL_FACTURA,
    LABEL_NOMBRE,
    LABEL_DIRECCION,
    LABEL_PAGO,
]


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return re.sub(r"[ \t]+", " ", text)


def _is_label(text: str) -> bool:
    return any(p.search(text) for p in LABEL_SET)


def _next_nonempty_strings(node: Tag, limit: int = 20) -> list[str]:
    values: list[str] = []
    for item in node.find_all_next(string=True, limit=40):
        if not isinstance(item, NavigableString):
            continue
        text = " ".join(str(item).split())
        if not text:
            continue
        values.append(text)
        if len(values) >= limit:
            break
    return values


def _value_after_label(soup: BeautifulSoup, pattern: re.Pattern[str]) -> Optional[str]:
    for el in soup.find_all(string=pattern):
        parent = el.parent
        if not isinstance(parent, Tag):
            continue
        full = " ".join(parent.get_text(" ", strip=True).split())
        remainder = pattern.sub("", full, count=1).strip(" :-")
        if remainder and not _is_label(remainder):
            return remainder
        for candidate in _next_nonempty_strings(parent):
            if pattern.search(candidate):
                candidate = pattern.sub("", candidate, count=1).strip(" :-")
                if not candidate:
                    continue
            if _is_label(candidate):
                break
            if SIN_FACTURA.search(candidate):
                break
            return candidate
    return None


def parse_money(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _estado_pago(text: str) -> Optional[str]:
    match = re.search(
        r"\b(NO PAGADA|PAGADA|VENCIDA|PENDIENTE|PAGO EXITOSO)\b",
        text,
        re.I,
    )
    if match:
        return match.group(1).upper()
    return None


def _search(text: str, pattern: str) -> Optional[str]:
    match = re.search(pattern, text, re.I | re.S)
    if not match:
        return None
    return " ".join(match.group(1).split())


def parse_factura_html(html: str) -> tuple[Optional[Factura], Optional[str]]:
    """Devuelve (factura, mensaje_sin_resultados)."""
    text = _visible_text(html)
    if LIMITE_CONSULTAS.search(text):
        return None, None
    if SIN_FACTURA.search(text):
        return None, "No se encuentran facturas pendientes por pagar para la matrícula"

    soup = BeautifulSoup(html, "lxml")

    fecha = _value_after_label(soup, LABEL_FECHA) or _search(
        text,
        r"FECHA\s+DE\s+(?:SUSPENSI[OÓ]N|VENCIMIENTO|CORTE)\s+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
    )
    periodo_raw = _value_after_label(soup, LABEL_PERIODO)
    periodo = None
    if periodo_raw:
        periodo = re.sub(
            r"^Periodo\s+de\s+facturaci[oó]n\s*",
            "",
            periodo_raw,
            flags=re.I,
        ).strip()
    if not periodo:
        periodo = _search(
            text,
            r"Periodo\s+de\s+facturaci[oó]n\s+([A-Za-zÁÉÍÓÚáéíóúñÑ]+(?:\s+del?\s+\d{4})?)",
        )

    matricula = _value_after_label(soup, LABEL_MATRICULA) or _search(
        text, r"N[ºO°.]?\s*MATR[IÍ]CULA\s+(\d{3,12})"
    )
    numero = _value_after_label(soup, LABEL_FACTURA) or _search(
        text, r"N[ÚU]MERO\s+DE\s+FACTURA\s+(\d+)"
    )
    if numero and not re.search(r"\d", numero):
        numero = _search(text, r"N[ÚU]MERO\s+DE\s+FACTURA\s+(\d+)")

    nombre = _value_after_label(soup, LABEL_NOMBRE) or _search(
        text, r"NOMBRE\s+DEL\s+TITULAR\s+([A-ZÁÉÍÓÚÑ ]{5,})"
    )
    direccion = _value_after_label(soup, LABEL_DIRECCION) or _search(
        text, r"DIRECCI[OÓ]N(?:\s+DEL\s+TITULAR)?\s+([^\n]{5,80})"
    )
    pago_fmt = _value_after_label(soup, LABEL_PAGO) or _search(
        text, r"(?:PAGO\s+TOTAL|TOTAL\s+A\s+PAGAR|VALOR\s+A\s+PAGAR)\s+(\$?[\d.,]+)"
    )

    if pago_fmt and re.search(r"\b(NO PAGADA|PAGADA|VENCIDA|PENDIENTE)\b", pago_fmt, re.I):
        pago_fmt = re.sub(
            r"\b(NO PAGADA|PAGADA|VENCIDA|PENDIENTE|PAGO EXITOSO)\b",
            "",
            pago_fmt,
            flags=re.I,
        ).strip()

    estado = _estado_pago(text)
    pago_total = parse_money(pago_fmt)

    if not any([fecha, matricula, numero, nombre, pago_total]):
        return None, None

    pagada = None
    if estado:
        pagada = estado.upper() in {"PAGADA", "PAGO EXITOSO", "PAGADO"}

    return (
        Factura(
            fecha_suspension=fecha,
            periodo_facturacion=periodo,
            matricula=matricula,
            numero_factura=numero,
            nombre_titular=nombre,
            direccion_titular=direccion,
            pago_total=pago_total,
            pago_total_formato=pago_fmt,
            estado_pago=estado,
            pagada=pagada,
        ),
        None,
    )


def ibal_block_message(html: str) -> Optional[str]:
    text = _visible_text(html)
    if LIMITE_CONSULTAS.search(text):
        return (
            "IBAL bloqueó la IP por exceso de consultas. "
            "Espera 10 a 15 minutos y vuelve a intentar una sola vez. "
            "Consultar en bucle alarga el bloqueo."
        )
    return None


def compact_text(html: str, limit: int = 400) -> str:
    return " ".join(_visible_text(html).split())[:limit]


def is_landing_page(html: str) -> bool:
    text = _visible_text(html)
    if SIN_FACTURA.search(text):
        return False
    if LABEL_FECHA.search(text) or LABEL_PAGO.search(text) or LABEL_FACTURA.search(text):
        return False
    return "Bienvenido al sistema de pagos" in text or "SISTEMA DE PAGOS IBAL" in text.upper()


def describe_empty_html(html: str) -> str:
    bloqueo = ibal_block_message(html)
    if bloqueo:
        return bloqueo
    text = _visible_text(html)
    lower = text.lower()
    if "the action you requested is not allowed" in lower:
        return "IBAL rechazó la sesión CSRF. Reintenta la consulta."
    if "captcha" in lower and any(w in lower for w in ("inválid", "invalid", "error", "fall")):
        return "IBAL rechazó el reCAPTCHA. Espera unos segundos e intenta de nuevo."
    if is_landing_page(html) or "Bienvenido al sistema de pagos" in text:
        return (
            "IBAL no procesó la consulta y devolvió la página de inicio. "
            "El reCAPTCHA del portal no validó. Espera 15 segundos y vuelve a consultar."
        )
    if "form_consulta_desktop" in html or "busca_desktop" in html:
        return (
            "IBAL devolvió el formulario de búsqueda sin tarjetas de factura. "
            "Normalmente el reCAPTCHA no validó desde el servidor. Espera 10 segundos y vuelve a consultar."
        )
    compact = " ".join(text.split())[:160]
    if compact:
        return f"IBAL respondió sin un formato de factura reconocible: {compact}"
    return "IBAL respondió sin un formato de factura reconocible."
