(function () {
  const match = window.location.pathname.match(/^\/admin\/socios\/(\d+)\/?$/);
  if (!match) return;

  const rentalSection = document.getElementById('renta-instalaciones');
  if (!rentalSection || document.getElementById('ventas-cargos-extra')) return;

  const memberId = match[1];
  const section = document.createElement('section');
  section.className = 'card';
  section.id = 'ventas-cargos-extra';
  section.innerHTML = `
    <div class="section-title">
      <div><span class="eyebrow">VENTAS Y ACTIVIDADES</span><h2>Ventas y cargos extra</h2></div>
      <span class="badge muted">Cargo al socio</span>
    </div>
    <p>Registra productos vendidos al socio o cargos por actividades de Damas y Leones. Los conceptos sin precio fijo permiten capturar el precio unitario.</p>
    <form id="extra-charge-form" class="compact-form" action="/admin/socios/${memberId}/pago" method="post">
      <label class="wide">Concepto
        <select id="extra-item" required>
          <option value="" data-price="0" data-manual="0" data-kind="">Seleccionar...</option>
          <optgroup label="Venta a socios">
            <option data-price="57" data-manual="0" data-kind="Venta" data-desc="Refresco 2.5 L">Refresco 2.5 L · $57 c/u</option>
            <option data-price="13" data-manual="0" data-kind="Venta" data-desc="Refresco 355 ml vidrio">Refresco 355 ml vidrio · $13 c/u</option>
            <option data-price="31" data-manual="0" data-kind="Venta" data-desc="Topo Chico 1.5 L">Topo Chico 1.5 L · $31 c/u</option>
            <option data-price="18" data-manual="0" data-kind="Venta" data-desc="Refresco no retornable 450 ml plástico">Refresco no retornable 450 ml plástico · $18 c/u</option>
            <option data-price="140" data-manual="0" data-kind="Venta" data-desc="Bolsa de hielo 25 kg">Bolsa de hielo 25 kg · $140 por bolsa</option>
            <option data-price="315" data-manual="0" data-kind="Venta" data-desc="TKT-L 20/2 · cartón c/20 botellas vidrio">TKT-L 20/2 · cartón c/20 botellas · $315</option>
            <option data-price="315" data-manual="0" data-kind="Venta" data-desc="Indio 20/2 · cartón c/20 botellas">Indio 20/2 · cartón c/20 botellas · $315</option>
            <option data-price="40" data-manual="0" data-kind="Venta" data-desc="Mantel redondo">Mantel redondo · $40 por pieza</option>
            <option data-price="40" data-manual="0" data-kind="Venta" data-desc="Mantel rectangular">Mantel rectangular · $40 por pieza</option>
            <option data-price="0" data-manual="1" data-kind="Cargo extra" data-desc="Uso de loza">Uso de loza · precio manual</option>
          </optgroup>
          <optgroup label="Actividades Damas y Leones">
            <option data-price="0" data-manual="1" data-kind="Actividad" data-desc="Venta de hamburguesas">Venta de hamburguesas · precio manual</option>
            <option data-price="0" data-manual="1" data-kind="Actividad" data-desc="Lotería · boleto">Lotería · boleto · precio manual</option>
            <option data-price="0" data-manual="1" data-kind="Actividad" data-desc="Útiles escolares">Útiles escolares · precio manual</option>
            <option data-price="0" data-manual="1" data-kind="Actividad" data-desc="Quiniela">Quiniela · precio manual</option>
            <option data-price="0" data-manual="1" data-kind="Actividad" data-desc="Torneo de golf">Torneo de golf · precio manual</option>
          </optgroup>
        </select>
      </label>
      <label>Fecha<input id="extra-date" type="date" required></label>
      <label>Cantidad<input id="extra-qty" type="number" min="1" step="1" value="1" required></label>
      <label>Precio unitario<input id="extra-unit-price" type="number" min="0" step="0.01" required></label>
      <label>Total calculado<input id="extra-total-view" type="text" readonly></label>
      <label>Método<select name="method" required><option>Efectivo</option><option>Transferencia</option><option>Tarjeta</option><option>Otro</option></select></label>
      <label class="wide">Nota adicional<input id="extra-note" type="text" placeholder="Opcional"></label>
      <input type="hidden" name="concept" id="extra-concept">
      <input type="hidden" name="amount" id="extra-amount">
      <input type="hidden" name="reference" id="extra-reference">
      <input type="hidden" name="pool_plan" value="">
      <input type="hidden" name="pool_start_date" value="">
      <button type="submit">Registrar pago</button>
      <button type="submit" class="secondary" formaction="/admin/socios/${memberId}/adeudo/agregar">Agregar al adeudo</button>
    </form>
    <small>Los precios fijos provienen de la lista de cargos adicionales. Uso de loza y actividades permiten definir el precio al momento del registro.</small>
  `;

  rentalSection.insertAdjacentElement('afterend', section);

  const form = document.getElementById('extra-charge-form');
  const item = document.getElementById('extra-item');
  const dateField = document.getElementById('extra-date');
  const qty = document.getElementById('extra-qty');
  const unitPrice = document.getElementById('extra-unit-price');
  const totalView = document.getElementById('extra-total-view');
  const concept = document.getElementById('extra-concept');
  const amount = document.getElementById('extra-amount');
  const reference = document.getElementById('extra-reference');
  const note = document.getElementById('extra-note');

  const now = new Date();
  const yyyy = now.getFullYear();
  const mm = String(now.getMonth() + 1).padStart(2, '0');
  const dd = String(now.getDate()).padStart(2, '0');
  dateField.value = `${yyyy}-${mm}-${dd}`;

  function refreshExtra() {
    const opt = item.options[item.selectedIndex];
    const manual = opt && opt.dataset.manual === '1';
    const fixedPrice = opt ? Number(opt.dataset.price || 0) : 0;
    const description = opt ? (opt.dataset.desc || '') : '';
    const kind = opt ? (opt.dataset.kind || '') : '';

    if (!manual) {
      unitPrice.value = fixedPrice ? fixedPrice.toFixed(2) : '';
      unitPrice.readOnly = true;
    } else {
      unitPrice.readOnly = false;
      if (item.dataset.lastSelected !== description) unitPrice.value = '';
    }
    item.dataset.lastSelected = description;

    const quantity = Math.max(1, parseInt(qty.value || '1', 10));
    const price = Math.max(0, Number(unitPrice.value || 0));
    const total = Math.round(price * quantity * 100) / 100;
    amount.value = total.toFixed(2);
    totalView.value = total ? total.toLocaleString('es-MX', {style: 'currency', currency: 'MXN'}) : '';

    const rawDate = dateField.value || '';
    const parts = rawDate.split('-');
    const dateText = parts.length === 3 ? `${parts[2]}/${parts[1]}/${parts[0]}` : rawDate;
    const prefix = kind === 'Venta' ? 'Venta a socio' : (kind === 'Actividad' ? 'Actividad Damas y Leones' : 'Cargo extra');
    concept.value = description ? `${prefix} · ${description} · ${quantity} x $${price.toFixed(2)} · ${dateText}`.slice(0, 180) : '';
    const extraNote = (note.value || '').trim();
    reference.value = (`Fecha: ${dateText}` + (extraNote ? ` · ${extraNote}` : '')).slice(0, 120);
  }

  item.addEventListener('change', refreshExtra);
  qty.addEventListener('input', refreshExtra);
  unitPrice.addEventListener('input', refreshExtra);
  dateField.addEventListener('change', refreshExtra);
  note.addEventListener('input', refreshExtra);

  form.addEventListener('submit', function (event) {
    refreshExtra();
    if (!item.value && !item.options[item.selectedIndex]?.dataset.desc) {
      event.preventDefault();
      alert('Selecciona un concepto.');
      return;
    }
    if (Number(amount.value || 0) <= 0) {
      event.preventDefault();
      alert('El importe debe ser mayor a cero.');
    }
  });

  refreshExtra();
})();
