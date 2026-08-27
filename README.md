# API de consulta de facturas IBAL

Consulta facturas pendientes del portal público [ibal.gov.co/pagos](https://ibal.gov.co/pagos/) usando el número de matrícula y las devuelve en JSON. Pensada para desplegarse en Railway.

## Qué devuelve

Los mismos datos de las tarjetas del portal:

| Campo | Descripción |
| --- | --- |
| `fecha_suspension` | Fecha de suspensión |
| `periodo_facturacion` | Periodo de facturación |
| `matricula` | Nº de matrícula |
| `numero_factura` | Número de factura |
| `nombre_titular` | Nombre del titular |
| `direccion_titular` | Dirección del titular |
| `pago_total` | Valor en pesos (número) |
| `pago_total_formato` | Valor como aparece en IBAL |
| `estado_pago` | `NO PAGADA`, `PAGADA`, etc. |
| `pagada` | `true` / `false` |

## Consulta en la web

Al abrir la raíz del servicio aparece un formulario para consultar por matrícula y ver las 4 tarjetas (fecha, factura, titular y pago):

```
https://TU-SERVICIO.up.railway.app/
```

## Endpoints

```
GET  /
GET  /health
GET  /api
GET  /api/v1/facturas/{matricula}
GET  /api/v1/consulta?matricula=24714
POST /api/v1/consulta
     { "matricula": "24714" }
```

Documentación interactiva: `/docs`

### Ejemplo de respuesta

```json
{
  "ok": true,
  "matricula_consultada": "24714",
  "encontrada": true,
  "mensaje": "Factura encontrada",
  "factura": {
    "fecha_suspension": "04/09/2026",
    "periodo_facturacion": "Julio del 2026",
    "matricula": "24714",
    "numero_factura": "22430310",
    "nombre_titular": "MARIA ISABEL SANCHEZ RODRIGUEZ",
    "direccion_titular": "VIA BOGOTA PICALEÑA",
    "pago_total": 303200,
    "pago_total_formato": "$303,200",
    "estado_pago": "NO PAGADA",
    "pagada": false
  },
  "motor": "browser"
}
```

Si no hay factura pendiente:

```json
{
  "ok": true,
  "matricula_consultada": "24714",
  "encontrada": false,
  "mensaje": "No se encuentran facturas pendientes por pagar para la matrícula",
  "factura": null,
  "motor": "browser"
}
```

## Uso

```bash
curl "https://TU-SERVICIO.up.railway.app/api/v1/facturas/24714"
```

Con API key (si configuraste `API_KEY`):

```bash
curl -H "X-API-Key: tu-clave" "https://TU-SERVICIO.up.railway.app/api/v1/facturas/24714"
```

## Despliegue en Railway

1. Crea un proyecto en [Railway](https://railway.app) y conecta este repositorio (o usa `railway up`).
2. Railway detecta el `Dockerfile` y levanta Chromium (necesario por el reCAPTCHA v3 del portal).
3. Variables de entorno opcionales:

| Variable | Default | Descripción |
| --- | --- | --- |
| `IBAL_ENGINE` | `browser` (en Docker) | `browser`, `http` o `auto` |
| `API_KEY` | vacío | Si se define, exige header `X-API-Key` |
| `CACHE_TTL_SECONDS` | `300` | Caché en segundos (`0` desactiva) |
| `CORS_ORIGINS` | `*` | Orígenes permitidos, separados por coma |
| `PORT` | lo asigna Railway | No hace falta configurarlo |

4. El healthcheck queda en `/health`.
5. Llama a `https://<tu-dominio>.up.railway.app/api/v1/facturas/{matricula}`.

En el panel de Railway asigna al menos **1 GB de RAM**; Chromium no corre bien con 512 MB.

## Desarrollo local

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --reload --port 8000
```

Abre http://localhost:8000/docs

Si no quieres instalar Chromium en local:

```bash
set IBAL_ENGINE=http
uvicorn app.main:app --reload --port 8000
```

`http` puede fallar si IBAL exige reCAPTCHA. En Railway usa `browser`.

## Cómo funciona

El portal de IBAL no publica un JSON oficial. Esta API:

1. Abre https://ibal.gov.co/pagos/
2. Envía el número de matrícula (el mismo formulario público)
3. Completa reCAPTCHA v3 y el token CSRF de CodeIgniter
4. Lee las tarjetas de resultado y las mapea a JSON

Los datos salen en tiempo real del portal de IBAL.
