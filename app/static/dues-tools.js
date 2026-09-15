(function () {
  const extraSelect = document.getElementById('extra-item');
  if (extraSelect && !extraSelect.querySelector('[data-dues-event-charge="1"]')) {
    const group = document.createElement('optgroup');
    group.label = 'Cargos adicionales de eventos';
    [
      ['Entrada a Posada', 'Entrada a Posada'],
      ['Entrada a Coronación', 'Entrada a Coronación'],
      ['Entrada a Cambio de Mesa Directiva', 'Entrada a Cambio de Mesa Directiva']
    ].forEach(([label, desc]) => {
      const option = document.createElement('option');
      option.textContent = label + ' · importe manual';
      option.dataset.price = '0';
      option.dataset.manual = '1';
      option.dataset.kind = 'Cargo extra';
      option.dataset.desc = desc;
      option.dataset.duesEventCharge = '1';
      group.appendChild(option);
    });
    extraSelect.appendChild(group);
  }

  document.querySelectorAll('a[href^="/recibo/"]').forEach(link => {
    const row = link.closest('tr');
    const alert = link.closest('.alert');
    const rowText = row ? row.innerText : '';
    const alertText = alert ? alert.innerText : '';
    const isDues = rowText.includes('Cuota de socio') || alertText.includes('Cuota registrada correctamente');
    if (!isDues) return;
    const match = link.getAttribute('href').match(/\/recibo\/(\d+)\.pdf/);
    if (match) link.setAttribute('href', `/recibo-cuota/${match[1]}.pdf`);
  });
})();
