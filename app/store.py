import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import settings
from app.models import ConsultaResponse

logger = logging.getLogger("store")

DATA_FILE = Path("data/consultas_cache.json")


def _now() -> float:
    return time.time()


def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _hour_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def _load() -> dict:
    if not DATA_FILE.exists():
        return {"live": {}, "items": {}}
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"live": {}, "items": {}}


def _save(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def get_cached(matricula: str, allow_stale: bool = False) -> Optional[ConsultaResponse]:
    data = _load()
    item = data.get("items", {}).get(matricula)
    if not item:
        return None
    age = _now() - float(item.get("ts") or 0)
    ttl = settings.cache_ttl_seconds
    stale = settings.cache_stale_seconds
    if age <= ttl:
        return ConsultaResponse.model_validate(item["payload"])
    if allow_stale and stale > 0 and age <= stale:
        payload = ConsultaResponse.model_validate(item["payload"])
        return payload.model_copy(
            update={
                "desde_cache": True,
                "cache_stale": True,
                "mensaje": (payload.mensaje or "Factura encontrada") + " (dato en caché, no se consultó IBAL)",
            }
        )
    return None


def store_cached(matricula: str, payload: ConsultaResponse) -> None:
    if settings.cache_ttl_seconds <= 0:
        return
    data = _load()
    items = data.setdefault("items", {})
    dumped = payload.model_dump()
    dumped["desde_cache"] = False
    dumped["cache_stale"] = False
    items[matricula] = {"ts": _now(), "payload": dumped}
    _save(data)


def live_counts() -> tuple[int, int]:
    data = _load()
    live = data.get("live") or {}
    day = live.get("count", 0) if live.get("day") == _day_key() else 0
    hour = live.get("hour_count", 0) if live.get("hour") == _hour_key() else 0
    return int(day), int(hour)


def can_hit_ibal() -> tuple[bool, str]:
    day, hour = live_counts()
    if day >= settings.ibal_max_live_per_day:
        return False, (
            f"Tope diario de consultas en vivo a IBAL ({settings.ibal_max_live_per_day}). "
            "El resto se sirve de caché. Mañana se reinicia el cupo."
        )
    if hour >= settings.ibal_max_live_per_hour:
        return False, (
            f"Tope por hora de consultas en vivo a IBAL ({settings.ibal_max_live_per_hour}). "
            "Usa caché o espera a la siguiente hora."
        )
    return True, ""


def register_live_hit() -> None:
    data = _load()
    live = data.setdefault("live", {})
    day = _day_key()
    hour = _hour_key()
    if live.get("day") != day:
        live["day"] = day
        live["count"] = 0
    if live.get("hour") != hour:
        live["hour"] = hour
        live["hour_count"] = 0
    live["count"] = int(live.get("count") or 0) + 1
    live["hour_count"] = int(live.get("hour_count") or 0) + 1
    _save(data)
    logger.info("Consultas IBAL en vivo hoy=%s hora=%s", live["count"], live["hour_count"])
