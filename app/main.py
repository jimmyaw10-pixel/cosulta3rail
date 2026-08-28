import logging
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field

from app.config import settings
from app.ibal import ConsultaError, consultar_factura, start_browser, stop_browser
from app.models import ConsultaResponse, ErrorResponse


class ConsultaRequest(BaseModel):
    matricula: str = Field(..., min_length=3, max_length=12, pattern=r"^\d+$")


class LoteRequest(BaseModel):
    matriculas: list[str] = Field(..., min_length=1, max_length=100)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = settings.ibal_engine.lower()
    if engine in {"browser", "auto"}:
        try:
            await start_browser()
        except Exception as exc:
            logger.warning("No se pudo iniciar Chromium al arrancar: %s", exc)
    yield
    await stop_browser()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Consulta facturas pendientes del portal público de pagos IBAL "
        "(https://ibal.gov.co/pagos/) a partir del número de matrícula."
    ),
    lifespan=lifespan,
)

origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

STATIC_DIR = FilePath(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, str):
        error = detail
    elif isinstance(detail, list):
        error = "; ".join(str(item) for item in detail)
    else:
        error = str(detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": error},
    )


def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    if not settings.api_key:
        return
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="API key inválida o ausente")


@app.get("/", include_in_schema=False)
async def web():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api", tags=["meta"])
async def api_info():
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "web": "/",
        "endpoints": {
            "consulta_path": "/api/v1/facturas/{matricula}",
            "consulta_query": "/api/v1/consulta?matricula=",
            "lote": "/api/v1/lote",
            "health": "/health",
        },
    }


@app.get("/health", tags=["meta"])
async def health():
    return {
        "ok": True,
        "engine": settings.ibal_engine,
        "captcha_solver": settings.captcha_solver,
        "captcha_configurado": bool(
            settings.captcha_api_key and settings.captcha_solver not in {"", "off", "browser"}
        ),
    }


@app.get(
    "/api/v1/facturas/{matricula}",
    response_model=ConsultaResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    tags=["facturas"],
    summary="Consultar factura por matrícula",
)
async def get_factura(
    matricula: str = Path(..., min_length=3, max_length=12, pattern=r"^\d+$"),
    _: None = Depends(require_api_key),
):
    return await _consultar(matricula)


@app.get(
    "/api/v1/consulta",
    response_model=ConsultaResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    tags=["facturas"],
    summary="Consultar factura (query string)",
)
async def get_consulta(
    matricula: str = Query(..., min_length=3, max_length=12, pattern=r"^\d+$"),
    _: None = Depends(require_api_key),
):
    return await _consultar(matricula)


@app.post(
    "/api/v1/consulta",
    response_model=ConsultaResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    tags=["facturas"],
    summary="Consultar factura (JSON)",
)
async def post_consulta(body: ConsultaRequest, _: None = Depends(require_api_key)):
    return await _consultar(body.matricula)


@app.post("/api/v1/lote", tags=["facturas"], summary="Consultar varias matrículas en vivo")
async def post_lote(body: LoteRequest, _: None = Depends(require_api_key)):
    resultados = []
    for raw in body.matriculas:
        matricula = "".join(ch for ch in str(raw) if ch.isdigit())
        if not (3 <= len(matricula) <= 12):
            resultados.append(
                {
                    "ok": False,
                    "matricula_consultada": str(raw),
                    "encontrada": False,
                    "mensaje": "Matrícula inválida",
                }
            )
            continue
        resultados.append((await _consultar(matricula)).model_dump())
    return {"ok": True, "total": len(resultados), "resultados": resultados}


async def _consultar(matricula: str) -> ConsultaResponse:
    try:
        return await consultar_factura(matricula)
    except ConsultaError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Error inesperado consultando IBAL")
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo consultar el portal IBAL: {exc}",
        ) from exc
