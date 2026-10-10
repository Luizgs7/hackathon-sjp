/* Mapa da equipe técnica: técnicos e chamados aguardando direcionamento (posições simuladas). */
(function () {
  var box = document.getElementById('equipe-mapa');
  if (!box) return;
  var map, layer, ready = null, fitted = false;
  function load() {
    if (ready) return ready;
    ready = new Promise(function (ok, fail) {
      var css = document.createElement('link'); css.rel = 'stylesheet'; css.href = '/static/vendor/leaflet/leaflet.css'; document.head.appendChild(css);
      var js = document.createElement('script'); js.src = '/static/vendor/leaflet/leaflet.js'; js.onload = ok; js.onerror = fail; document.head.appendChild(js);
    });
    return ready;
  }
  function esc(t) { var d = document.createElement('div'); d.textContent = t == null ? '' : t; return d.innerHTML; }
  function draw(d) {
    load().then(function () {
      if (!map) {
        map = L.map('equipe-mapa', { scrollWheelZoom: false }).setView(d.base, 14);
        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap' }).addTo(map);
        layer = L.layerGroup().addTo(map);
      }
      layer.clearLayers();
      var pts = [];
      d.tecnicos.forEach(function (t) {
        var cor = t.disponivel ? '#2563eb' : '#6b7280';
        L.circleMarker(t.posicao, { radius: 9, color: '#fff', weight: 3, fillColor: cor, fillOpacity: 1 }).addTo(layer)
          .bindTooltip('<b>' + esc(t.nome) + '</b><br>' + t.total + ' no backlog' + (t.atual ? '<br>Atendendo #' + t.atual.id + ' · ' + esc(t.atual.local) : '') + (t.disponivel ? '' : '<br>Inativo'));
        pts.push(t.posicao);
      });
      d.pendentes.forEach(function (p) {
        L.circleMarker(p.alvo, { radius: 8, color: '#b45309', weight: 2, fillColor: '#f59e0b', fillOpacity: 0.95 }).addTo(layer)
          .bindTooltip('<b>#' + p.id + ' · ' + esc(p.titulo) + '</b><br>' + esc(p.local) + ' · ' + esc(p.prio));
        pts.push(p.alvo);
      });
      if (!fitted && pts.length) { map.fitBounds(L.latLngBounds(pts), { padding: [30, 30], maxZoom: 15 }); fitted = true; }
    }).catch(function () {});
  }
  function tick() {
    fetch(box.dataset.url, { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) draw(d); })
      .catch(function () {});
  }
  tick();
  setInterval(tick, 5000);
})();
