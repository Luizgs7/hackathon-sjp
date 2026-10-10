/* Mapa simulado do técnico: consulta /api/rastro a cada 5 s e move o marcador. Leaflet local, carregado sob demanda. */
(function () {
  var box = document.getElementById('mapa-tecnico');
  if (!box) return;
  var map, marker, started = false, ready = null;
  function load() {
    if (ready) return ready;
    ready = new Promise(function (ok, fail) {
      var css = document.createElement('link');
      css.rel = 'stylesheet'; css.href = '/static/vendor/leaflet/leaflet.css';
      document.head.appendChild(css);
      var js = document.createElement('script');
      js.src = '/static/vendor/leaflet/leaflet.js'; js.onload = ok; js.onerror = fail;
      document.head.appendChild(js);
    });
    return ready;
  }
  function eta(d) { return d.chegou ? 'Chegou ao local' : 'Chega em ~' + d.eta_min + ' min'; }
  function draw(d) {
    if (!d.posicao) { box.hidden = true; return; }
    box.hidden = false;
    document.getElementById('mapa-eta').textContent = eta(d);
    load().then(function () {
      if (!started) {
        started = true;
        map = L.map('mapa-canvas', { scrollWheelZoom: false });
        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap' }).addTo(map);
        L.polyline(d.caminho, { color: '#2563eb', weight: 4, opacity: .6, dashArray: '6 8' }).addTo(map);
        L.circleMarker(d.destino, { radius: 8, color: '#dc2626', fillOpacity: .9 }).addTo(map).bindTooltip('Local do atendimento');
        marker = L.circleMarker(d.posicao, { radius: 9, color: '#fff', weight: 3, fillColor: '#2563eb', fillOpacity: 1 }).addTo(map);
        map.fitBounds(L.polyline(d.caminho).getBounds(), { padding: [30, 30] });
        var ruas = L.polyline([], { color: '#2563eb', weight: 4, opacity: .85 }).addTo(map);
        if (window.rotaPelasRuas) window.rotaPelasRuas([d.caminho[0], d.destino], function (linha) { ruas.setLatLngs(linha); });
      } else { marker.setLatLng(d.posicao); }
    }).catch(function () {});
  }
  function tick() {
    fetch(box.dataset.url, { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : { ativo: false }; })
      .then(draw).catch(function () {});
  }
  tick(); setInterval(tick, 5000);
})();
