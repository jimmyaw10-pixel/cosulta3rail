import re
from typing import Optional

from bs4 import BeautifulSoup, NavigableString, Tag

from app.models import Factura

LABEL_FECHA = re.compile(r"FECHA\s+DE\s+SUSPENSI[OÓ]N", re.I)
LABEL_PERIODO = re.compile(r"Periodo\s+de\s+facturaci[oó]n", re.I)
LABEL_MATRICULA = re.compile(r"N[ºO°]?\s*MATR[IÍ]CULA", re.I)
LABEL_FACTURA = re.compile(r"N[ÚU]MERO\s+DE\s+FACTURA", re.I)
LABEL_NOMBRE = re.compile(r"NOMBRE\s+DEL\s+TITULAR", re.I)
LABEL_DIRECCION = re.compile(r"DIRECCI[OÓ]N\s+DEL\s+TITULAR", re.I)
LABEL_PAGO = re.compile(r"PAGO\s+TOTAL", re.I)

SIN_FACTURA = re.compile(
    r"No se encue?ntran facturas pendientes por pagar",
    re.I,
)
RECAPTCHA_FAIL = re.compile(r"recaptcha|captcha", re.I)
CSRF_FAIL = re.compile(r"requested is not allowed|csrf", re.I)

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


def _estado_pago(text: str, soup: BeautifulSoup) -> Optional[str]:
    bloque = _value_after_label(soup, LABEL_PAGO) or ""
    ventana = f"{bloque}\n{text}"
    match = re.search(
        r"\b(NO PAGADA|PAGADA|VENCIDA|PENDIENTE|PAGO EXITOSO)\b",
        ventana,
        re.I,
    )
    if match:
        return match.group(1).upper()
    return None


def parse_factura_html(html: str) -> tuple[Optional[Factura], Optional[str]]:
    """Devuelve (factura, mensaje_sin_resultados)."""
    text = _visible_text(html)
    if SIN_FACTURA.search(text):
        return None, "No se encuentran facturas pendientes por pagar para la matrícula"

    soup = BeautifulSoup(html, "lxml")

    fecha = _value_after_label(soup, LABEL_FECHA)
    periodo_raw = _value_after_label(soup, LABEL_PERIODO)
    periodo = None
    if periodo_raw:
        periodo = re.sub(r"^Periodo\s+de\s+facturaci[oó]n\s*", "", periodo_raw, flags=re.I).strip()

    if not fecha:
        m = re.search(
            r"FECHA\s+DE\s+SUSPENSI[OÓ]N\s+(\d{1,2}/\d{1,2}/\d{4})",
            text,
            re.I,
        )
        if m:
            fecha = m.group(1)

    if not periodo:
        m = re.search(
            r"Periodo\s+de\s+facturaci[oó]n\s+([A-Za-zÁÉÍÓÚáéíóúñÑ]+(?:\s+del?\s+\d{4})?)",
            text,
            re.I,
        )
        if m:
            periodo = m.group(1).strip()

    matricula = _value_after_label(soup, LABEL_MATRICULA)
    numero = _value_after_label(soup, LABEL_FACTURA)
    nombre = _value_after_label(soup, LABEL_NOMBRE)
    direccion = _value_after_label(soup, LABEL_DIRECCION)
    pago_fmt = _value_after_label(soup, LABEL_PAGO)

    if pago_fmt and re.search(r"\b(NO PAGADA|PAGADA|VENCIDA|PENDIENTE)\b", pago_fmt, re.I):
        pago_fmt = re.sub(
            r"\b(NO PAGADA|PAGADA|VENCIDA|PENDIENTE|PAGO EXITOSO)\b",
            "",
            pago_fmt,
            flags=re.I,
        ).strip()

    if not matricula:
        m = re.search(r"N[ºO°]?\s*MATR[IÍ]CULA\s+(\d+)", text, re.I)
        if m:
            matricula = m.group(1)
    if not numero:
        m = re.search(r"N[ÚU]MERO\s+DE\s+FACTURA\s+(\d+)", text, re.I)
        if m:
            numero = m.group(1)
    if not pago_fmt:
        m = re.search(r"PAGO\s+TOTAL\s+(\$?[\d.,]+)", text, re.I)
        if m:
            pago_fmt = m.group(1)

    estado = _estado_pago(text, soup)
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


def detect_block_reason(html: str) -> Optional[str]:
    text = _visible_text(html)
    if CSRF_FAIL.search(text) and "csrf_test_name" not in html:
        return "csrf"
    if "The action you requested is not allowed" in text:
        return "csrf"
    if RECAPTCHA_FAIL.search(text) and "g-recaptcha-response" not in html.lower():
        return "recaptcha"
    return None
