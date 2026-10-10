/* Histórico pessoal do protótipo: cópias assinadas, sem recriar contadores ao consultar. */
(() => {
  const key = 'dataforge.conversasResolvidas.v1';
  const list = document.getElementById('lista-conversas');
  if (!list) return;
  let entries = [];
  try {
    const stored = JSON.parse(localStorage.getItem(key) || '[]');
    if (Array.isArray(stored)) entries = stored.filter(x => typeof x.id === 'string' && typeof x.estado === 'string');
    const finished = document.querySelector('[data-conversa-resolvida]');
    const state = document.getElementById('estado-chat');
    if (finished && state?.value) {
      const old = entries.find(x => x.id === finished.dataset.conversaResolvida);
      const entry = {id: finished.dataset.conversaResolvida, estado: state.value,
        titulo: document.querySelector('#conversa .df-bubble.me')?.textContent.trim().slice(0, 90) || 'Conversa resolvida',
        data: old?.data || new Date().toISOString()};
      entries = [entry, ...entries.filter(x => x.id !== entry.id)];
      localStorage.setItem(key, JSON.stringify(entries));
    }
  } catch (_) {
    const warning = document.createElement('p');
    warning.className = 'df-error'; warning.setAttribute('role', 'status');
    warning.textContent = 'Não foi possível salvar o histórico neste navegador.';
    list.append(warning);
  }
  for (const entry of entries) {
    const form = document.createElement('form');
    form.method = 'post'; form.action = '/servidor/historico';
    const input = document.createElement('input');
    input.type = 'hidden'; input.name = 'estado_chat'; input.value = entry.estado;
    const button = document.createElement('button');
    button.type = 'submit'; button.className = 'df-list-item df-wide df-history-item';
    const title = document.createElement('b'); title.textContent = entry.titulo;
    const details = document.createElement('small');
    details.textContent = 'Resolvida sem chamado · ' + new Date(entry.data).toLocaleDateString('pt-BR');
    button.append(title, details); form.append(input, button); list.append(form);
  }
  if (!entries.length) {
    const empty = document.createElement('small'); empty.className = 'df-muted';
    empty.textContent = 'Nenhuma conversa resolvida ainda.'; list.append(empty);
  }
})();
