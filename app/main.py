import logging
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field

from app import store
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
            "estado": "/api/v1/estado",
            "health": "/health",
        },
    }


@app.get("/health", tags=["meta"])
async def health():
    day, hour = store.live_counts()
    return {
        "ok": True,
        "engine": settings.ibal_engine,
        "ibal_vivo_hoy": day,
        "ibal_vivo_hora": hour,
        "tope_dia": settings.ibal_max_live_per_day,
        "tope_hora": settings.ibal_max_live_per_hour,
    }


@app.get("/api/v1/estado", tags=["meta"])
async def estado(_: None = Depends(require_api_key)):
    day, hour = store.live_counts()
    return {
        "ok": True,
        "consultas_vivo_hoy": day,
        "consultas_vivo_hora": hour,
        "tope_dia": settings.ibal_max_live_per_day,
        "tope_hora": settings.ibal_max_live_per_hour,
        "cache_ttl_segundos": settings.cache_ttl_seconds,
        "cache_stale_segundos": settings.cache_stale_seconds,
        "intervalo_minimo_segundos": settings.ibal_min_interval_seconds,
    }


@app.get(
    "/api/v1/facturas/{matricula}",
    response_model=ConsultaResponse,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    tags=["facturas"],
    summary="Consultar factura por matrícula",
)
async def get_factura(
    matricula: str = Path(..., min_length=3, max_length=12, pattern=r"^\d+$"),
    force: bool = Query(False, description="true fuerza consulta en vivo a IBAL"),
    _: None = Depends(require_api_key),
):
    return await _consultar(matricula, force=force)


@app.get(
    "/api/v1/consulta",
    response_model=ConsultaResponse,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    tags=["facturas"],
    summary="Consultar factura (query string)",
)
async def get_consulta(
    matricula: str = Query(..., min_length=3, max_length=12, pattern=r"^\d+$"),
    force: bool = Query(False),
    _: None = Depends(require_api_key),
):
    return await _consultar(matricula, force=force)


@app.post(
    "/api/v1/consulta",
    response_model=ConsultaResponse,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    tags=["facturas"],
    summary="Consultar factura (JSON)",
)
async def post_consulta(body: ConsultaRequest, _: None = Depends(require_api_key)):
    return await _consultar(body.matricula)


@app.post("/api/v1/lote", tags=["facturas"], summary="Consultar varias matrículas usando caché")
async def post_lote(body: LoteRequest, _: None = Depends(require_api_key)):
    resultados = []
    vivos_en_este_llamado = 0
    for raw in body.matriculas:
        matricula = "".join(ch for ch in str(raw) if ch.isdigit())
        if not (3 <= len(matricula) <= 12):
            resultados.append(
                {
                    "ok": False,
                    "matricula_consultada": str(raw),
                    "encontrada": False,
                    "mensaje": "Matrícula inválida",
                    "desde_cache": False,
                }
            )
            continue
        fresco = store.get_cached(matricula, allow_stale=False)
        if fresco:
            resultados.append(
                fresco.model_copy(update={"desde_cache": True, "cache_stale": False}).model_dump()
            )
            continue
        if vivos_en_este_llamado >= 1:
            viejo = store.get_cached(matricula, allow_stale=True)
            if viejo:
                resultados.append(viejo.model_dump())
            else:
                resultados.append(
                    {
                        "ok": False,
                        "matricula_consultada": matricula,
                        "encontrada": False,
                        "mensaje": "Sin caché y sin cupo en vivo en este lote. Consulta esta matrícula más tarde.",
                        "desde_cache": False,
                    }
                )
            continue
        resultados.append((await _consultar(matricula)).model_dump())
        vivos_en_este_llamado += 1
    day, hour = store.live_counts()
    return {
        "ok": True,
        "total": len(resultados),
        "consultas_vivo_hoy": day,
        "consultas_vivo_hora": hour,
        "resultados": resultados,
    }


async def _consultar(matricula: str, force: bool = False) -> ConsultaResponse:
    if not force:
        fresco = store.get_cached(matricula, allow_stale=False)
        if fresco:
            return fresco.model_copy(update={"desde_cache": True, "cache_stale": False})

    permitido, razon = store.can_hit_ibal()
    if not permitido:
        viejo = store.get_cached(matricula, allow_stale=True)
        if viejo:
            return viejo
        raise HTTPException(status_code=429, detail=razon)

    try:
        result = await consultar_factura(matricula)
    except ConsultaError as exc:
        if exc.status_code == 429:
            from app.ibal import _marcar_limite_ibal

            _marcar_limite_ibal()
            viejo = store.get_cached(matricula, allow_stale=True)
            if viejo:
                return viejo
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Error inesperado consultando IBAL")
        viejo = store.get_cached(matricula, allow_stale=True)
        if viejo:
            return viejo
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo consultar el portal IBAL: {exc}",
        ) from exc
    if result.ok:
        store.store_cached(matricula, result)
    return result
