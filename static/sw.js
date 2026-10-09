// Service Worker: recebe Web Push e abre a missão ao tocar na notificação.
self.addEventListener('push', (event) => {
  let d = { title: 'Missões SJP', body: 'Nova atualização', url: '/campo' };
  try { d = Object.assign(d, event.data.json()); } catch (e) { /* payload vazio */ }
  event.waitUntil(self.registration.showNotification(d.title, {
    body: d.body, data: { url: d.url }, tag: d.url, renotify: true, requireInteraction: true,
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/campo';
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then((lista) => {
    for (const c of lista) { if ('focus' in c) { c.navigate(url); return c.focus(); } }
    return clients.openWindow(url);
  }));
});
