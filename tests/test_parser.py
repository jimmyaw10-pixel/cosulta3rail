from app.parser import parse_factura_html, parse_money


SAMPLE_HTML = """
<html><body>
  <div class="row">
    <div class="col-xl-3">
      <div class="text-xs font-weight-bold text-uppercase mb-1">FECHA DE SUSPENSIÓN</div>
      <div class="h5 mb-0 font-weight-bold">04/09/2026</div>
      <div>Periodo de facturación Julio del 2026</div>
    </div>
    <div class="col-xl-3">
      <div class="text-xs font-weight-bold text-uppercase mb-1">Nº MATRÍCULA</div>
      <div class="h5 mb-0 font-weight-bold">24714</div>
      <div class="text-xs font-weight-bold text-uppercase mb-1">NÚMERO DE FACTURA</div>
      <div class="h5 mb-0 font-weight-bold">22430310</div>
    </div>
    <div class="col-xl-3">
      <div class="text-xs font-weight-bold text-uppercase mb-1">NOMBRE DEL TITULAR</div>
      <div>MARIA ISABEL SANCHEZ RODRIGUEZ</div>
      <div class="text-xs font-weight-bold text-uppercase mb-1">DIRECCIÓN DEL TITULAR</div>
      <div>VIA BOGOTA PICALEÑA</div>
    </div>
    <div class="col-xl-3">
      <div class="text-xs font-weight-bold text-uppercase mb-1">PAGO TOTAL</div>
      <div class="h5 mb-0 font-weight-bold">$303,200</div>
      <div style="color:red">NO PAGADA</div>
    </div>
  </div>
</body></html>
"""


def test_parse_money():
    assert parse_money("$303,200") == 303200
    assert parse_money("$303.200") == 303200


def test_parse_cards():
    factura, msg = parse_factura_html(SAMPLE_HTML)
    assert msg is None
    assert factura is not None
    assert factura.fecha_suspension == "04/09/2026"
    assert factura.periodo_facturacion == "Julio del 2026"
    assert factura.matricula == "24714"
    assert factura.numero_factura == "22430310"
    assert factura.nombre_titular == "MARIA ISABEL SANCHEZ RODRIGUEZ"
    assert factura.direccion_titular == "VIA BOGOTA PICALEÑA"
    assert factura.pago_total == 303200
    assert factura.estado_pago == "NO PAGADA"
    assert factura.pagada is False


def test_sin_facturas():
    html = "<html><body>No se encuetran facturas pendientes por pagar para la matrícula</body></html>"
    factura, msg = parse_factura_html(html)
    assert factura is None
    assert msg is not None


if __name__ == "__main__":
    test_parse_money()
    test_parse_cards()
    test_sin_facturas()
    print("parser ok")
