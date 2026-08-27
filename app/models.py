from typing import Optional

from pydantic import BaseModel, Field


class Factura(BaseModel):
    fecha_suspension: Optional[str] = Field(
        None, description="Fecha de suspensión del servicio, ej. 04/09/2026"
    )
    periodo_facturacion: Optional[str] = Field(
        None, description="Periodo de facturación, ej. Julio del 2026"
    )
    matricula: Optional[str] = Field(None, description="Número de matrícula IBAL")
    numero_factura: Optional[str] = Field(None, description="Número de factura")
    nombre_titular: Optional[str] = Field(None, description="Nombre del titular")
    direccion_titular: Optional[str] = Field(None, description="Dirección del predio")
    pago_total: Optional[int] = Field(None, description="Valor a pagar en pesos, sin formato")
    pago_total_formato: Optional[str] = Field(None, description="Valor a pagar como aparece en IBAL")
    estado_pago: Optional[str] = Field(None, description="Ej. NO PAGADA, PAGADA")
    pagada: Optional[bool] = Field(None, description="true si el estado indica factura pagada")


class ConsultaResponse(BaseModel):
    ok: bool
    matricula_consultada: str
    encontrada: bool
    mensaje: str
    factura: Optional[Factura] = None
    motor: str = Field(description="Motor usado: http o browser")


class ErrorResponse(BaseModel):
    ok: bool = False
    error: str
    detalle: Optional[str] = None
