(function () {
  const memberMatch = window.location.pathname.match(/^\/admin\/socios\/\d+\/?$/);
  if (!memberMatch) return;

  const money = (value) => Number(value || 0).toLocaleString('es-MX', {
    style: 'currency',
    currency: 'MXN',
    minimumFractionDigits: 2
  });

  function updateRentalOptions(prices) {
    const select = document.getElementById('rental-item');
    if (!select) return;
    select.querySelectorAll('option[data-desc]').forEach((opt) => {
      const desc = opt.dataset.desc || '';
      if (!desc || prices[desc] === undefined) return;
      const price = Number(prices[desc] || 0);
      opt.dataset.price = String(price);
      const unit = opt.dataset.unit || 'fijo';
      const suffix = unit === 'hora' ? ' por hora' : (unit === 'unidad' ? ' c/u' : '');
      opt.textContent = `${desc} · ${money(price)}${suffix}`;
    });
    select.dispatchEvent(new Event('change', {bubbles: true}));
  }

  function updateSalesOptions(prices) {
    const select = document.getElementById('extra-item');
    if (!select) return;
    select.querySelectorAll('option[data-desc]').forEach((opt) => {
      const desc = opt.dataset.desc || '';
      if (!desc || prices[desc] === undefined) return;
      const price = Number(prices[desc] || 0);
      opt.dataset.price = String(price);
      opt.dataset.manual = price > 0 ? '0' : '1';
      opt.textContent = price > 0 ? `${desc} · ${money(price)}` : `${desc} · precio manual`;
    });
    select.dispatchEvent(new Event('change', {bubbles: true}));
  }

  fetch('/admin/precios/data', {credentials: 'same-origin'})
    .then((response) => {
      if (!response.ok) throw new Error('No fue posible cargar precios');
      return response.json();
    })
    .then((data) => {
      updateRentalOptions(data.rentals || {});
      updateSalesOptions(data.sales || {});
    })
    .catch(() => {
      // Si no se puede consultar la configuración, permanecen los precios
      // visibles en el HTML como respaldo y el formulario sigue funcionando.
    });
})();
