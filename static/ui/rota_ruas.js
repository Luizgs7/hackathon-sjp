/* Percurso pelas ruas: pede a rota ao OSRM (dados do OpenStreetMap). Se o serviço não responder, mantém o traçado simples. */
window.rotaPelasRuas = function (pontos, ok) {
  if (!pontos || pontos.length < 2 || !window.fetch) return;
  var coords = pontos.map(function (p) { return p[1].toFixed(6) + ',' + p[0].toFixed(6); }).join(';');
  var ctrl = window.AbortController ? new AbortController() : null;
  var t = setTimeout(function () { if (ctrl) ctrl.abort(); }, 8000);
  fetch('https://router.project-osrm.org/route/v1/driving/' + coords + '?overview=full&geometries=geojson', ctrl ? { signal: ctrl.signal } : {})
    .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
    .then(function (j) {
      clearTimeout(t);
      var g = j && j.routes && j.routes[0] && j.routes[0].geometry;
      if (g && g.coordinates && g.coordinates.length > 1) ok(g.coordinates.map(function (c) { return [c[1], c[0]]; }));
    })
    .catch(function () { clearTimeout(t); });
};
