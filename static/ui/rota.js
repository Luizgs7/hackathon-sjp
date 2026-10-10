/* Rota do técnico: paradas numeradas e percurso no mapa (posições simuladas). */
(function () {
  var box = document.getElementById('rota-mapa');
  if (!box) return;
  function load() {
    return new Promise(function (ok, fail) {
      var css = document.createElement('link'); css.rel = 'stylesheet'; css.href = '/static/vendor/leaflet/leaflet.css'; document.head.appendChild(css);
      var js = document.createElement('script'); js.src = '/static/vendor/leaflet/leaflet.js'; js.onload = ok; js.onerror = fail; document.head.appendChild(js);
    });
  }
  function esc(t) { var d = document.createElement('div'); d.textContent = t == null ? '' : t; return d.innerHTML; }
  fetch(box.dataset.url, { credentials: 'same-origin', cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      return load().then(function () {
        var map = L.map('rota-mapa', { scrollWheelZoom: false });
        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap' }).addTo(map);
        var base = L.polyline(d.caminho, { color: '#2563eb', weight: 5, opacity: 0.5, dashArray: '2 8' }).addTo(map);
        L.circleMarker(d.origem, { radius: 9, color: '#fff', weight: 3, fillColor: '#16a34a', fillOpacity: 1 }).addTo(map).bindTooltip('Você está aqui');
        d.paradas.forEach(function (p, i) {
          var icone = L.divIcon({ className: '', html: '<span class="df-rota-pino">' + (i + 1) + '</span>', iconSize: [30, 30], iconAnchor: [15, 15] });
          L.marker(p.ponto, { icon: icone }).addTo(map).bindTooltip('<b>#' + p.id + ' · ' + esc(p.titulo) + '</b><br>' + esc(p.local));
        });
        map.fitBounds(L.latLngBounds(d.caminho), { padding: [30, 30] });
        var ruas = L.polyline([], { color: '#2563eb', weight: 5, opacity: 0.85 }).addTo(map);
        window.rotaPelasRuas([d.origem].concat(d.paradas.map(function (p) { return p.ponto; })), function (linha) {
          ruas.setLatLngs(linha); map.removeLayer(base); map.fitBounds(ruas.getBounds(), { padding: [30, 30] });
        });
      });
    })
    .catch(function () {});
})();
