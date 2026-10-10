/* Arrastar e soltar no quadro do gestor.
   Soltar só abre a ação certa (modal); quem muda o status é o endpoint existente, que aplica as regras de transição. */
(() => {
  const dlg = document.getElementById('modal-mover');
  if (!dlg) return;
  const corpo = document.getElementById('modal-mover-corpo');
  const titulo = document.getElementById('modal-mover-titulo');
  const aviso = document.getElementById('quadro-aviso');
  const ROTULOS = { encaminhado: 'Encaminhado (a um técnico)', executado: 'Executado (resolver no atendimento)', cancelado: 'Cancelado' };
  let arrastando = null;

  const quadro = () => document.getElementById('quadro');
  const alvosDe = el => (el.dataset.alvos || '').split(' ').filter(Boolean);
  const atualizarQuadro = () => {
    if (!window.htmx || !quadro()) return;
    const values = {};
    document.querySelectorAll('[name=setor_id],[name=q]').forEach(i => { if (i.value) values[i.name] = i.value; });
    htmx.ajax('GET', '/gestor/quadro', { target: '#quadro', swap: 'innerHTML', values });
  };
  const anunciar = txt => { if (aviso) aviso.textContent = txt; };
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  function limpar() {
    document.body.classList.remove('df-arrastando');
    document.querySelectorAll('.df-alvo,.df-sem-alvo,.df-dnd-card.df-em-arraste').forEach(e => e.classList.remove('df-alvo', 'df-sem-alvo', 'df-em-arraste'));
    arrastando = null;
    pararRolagem();
  }

  function abrirFormulario(tid, para) {
    titulo.textContent = 'Mover chamado #' + tid;
    corpo.innerHTML = '<p class="df-muted" role="status">Carregando…</p>';
    if (!dlg.open) dlg.showModal();
    fetch('/tarefa/' + tid + '/mover?para=' + encodeURIComponent(para), { headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' })
      .then(async r => {
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.erro || 'Não foi possível abrir a ação.'); }
        return r.text();
      })
      .then(html => { corpo.innerHTML = html; const f = corpo.querySelector('select,textarea,input'); if (f) f.focus(); })
      .catch(e => { corpo.innerHTML = '<p class="df-error" role="alert">' + esc(e.message) + '</p><div class="df-form-actions"><button type="button" class="df-button df-ghost df-size-medium" data-dialog-close><span class="df-button-content">Fechar</span></button></div>'; });
  }

  function escolherDestino(card) {
    const tid = card.dataset.tid, alvos = alvosDe(card);
    if (alvos.length === 1) { abrirFormulario(tid, alvos[0]); return; }
    titulo.textContent = 'Mover chamado #' + tid;
    corpo.innerHTML = '<p class="df-muted">Para onde mover?</p><div class="df-stack">' +
      alvos.map(a => '<button type="button" class="df-button df-secondary df-size-medium df-wide" data-destino="' + a + '"><span class="df-button-content">' + esc(ROTULOS[a] || a) + '</span></button>').join('') + '</div>';
    corpo.querySelectorAll('[data-destino]').forEach(b => b.addEventListener('click', () => abrirFormulario(tid, b.dataset.destino)));
    dlg.showModal();
  }

  document.addEventListener('dragstart', e => {
    const card = e.target.closest && e.target.closest('.df-dnd-card');
    if (!card || !alvosDe(card).length) return;
    arrastando = card;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', card.dataset.tid);
    const alvos = alvosDe(card);
    document.body.classList.add('df-arrastando');
    document.querySelectorAll('.df-board-col').forEach(col => col.classList.add(alvos.includes(col.dataset.col) ? 'df-alvo' : 'df-sem-alvo'));
    iniciarRolagem();
    requestAnimationFrame(() => card.classList.add('df-em-arraste'));  // mexer no próprio cartão no mesmo instante cancela o arrasto
  });
  document.addEventListener('dragend', limpar);
  const colAceita = e => {
    const col = arrastando && e.target.closest && e.target.closest('.df-board-col');
    return col && alvosDe(arrastando).includes(col.dataset.col) ? col : null;
  };
  // o quadro é mais largo que a tela: perto das bordas, rola sozinho (por temporizador) para alcançar as colunas do outro lado
  let ultimoX = null, timerRolar = null;
  const rolar = () => {
    const q = document.querySelector('.df-board');
    if (!q || ultimoX === null) return;
    const r = q.getBoundingClientRect(), borda = 100;
    if (ultimoX > r.right - borda) q.scrollLeft += 24;
    else if (ultimoX < r.left + borda) q.scrollLeft -= 24;
  };
  const snap = v => { const q = document.querySelector('.df-board'); if (q) q.style.scrollSnapType = v; };  // o snap desfaria cada passo
  const iniciarRolagem = () => { snap('none'); ultimoX = null; clearInterval(timerRolar); timerRolar = setInterval(rolar, 30); };
  const pararRolagem = () => { snap(''); clearInterval(timerRolar); timerRolar = null; ultimoX = null; };
  document.addEventListener('dragover', e => {
    ultimoX = e.clientX;
    const col = colAceita(e);
    if (col) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; col.classList.add('df-alvo-sobre'); }
  });
  document.addEventListener('dragleave', e => {
    const col = e.target.closest && e.target.closest('.df-board-col');
    if (col && !col.contains(e.relatedTarget)) col.classList.remove('df-alvo-sobre');
  });
  document.addEventListener('drop', e => {
    const col = colAceita(e);
    if (!col) return;
    e.preventDefault();
    const tid = arrastando.dataset.tid, para = col.dataset.col;
    document.querySelectorAll('.df-alvo-sobre').forEach(x => x.classList.remove('df-alvo-sobre'));
    limpar();
    abrirFormulario(tid, para);
  });

  document.addEventListener('click', e => {
    const b = e.target.closest && e.target.closest('[data-mover-abrir]');
    if (b) escolherDestino(b.closest('.df-dnd-card'));
  });

  // envio do modal: POST ao endpoint existente; sucesso volta como redirecionamento (303), erro como JSON
  document.addEventListener('submit', async e => {
    const f = e.target.closest && e.target.closest('[data-mover-form]');
    if (!f) return;
    e.preventDefault();
    const erro = f.querySelector('[data-mover-erro]'), botao = f.querySelector('button[type=submit]');
    erro.hidden = true;
    botao.disabled = true;
    try {
      const r = await fetch(f.action, { method: 'POST', body: new FormData(f), redirect: 'manual', credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } });
      if (r.type === 'opaqueredirect' || r.ok) {
        dlg.close();
        anunciar('Chamado #' + f.dataset.tid + ' movido para ' + f.dataset.destino + '.');
        atualizarQuadro();
      } else {
        const j = await r.json().catch(() => ({}));
        erro.textContent = j.erro || 'Não foi possível mover o chamado.';
        erro.hidden = false;
      }
    } catch (_) {
      erro.textContent = 'Falha de conexão. Tente novamente.';
      erro.hidden = false;
    } finally { botao.disabled = false; }
  });

  // o polling de 3 s trocaria o quadro no meio do arrasto (ou com o modal aberto): pausa nesses casos
  document.body.addEventListener('htmx:beforeRequest', e => {
    const q = quadro();
    if (q && e.detail.elt === q && q.querySelector('.df-board') && (document.body.classList.contains('df-arrastando') || dlg.open)) e.preventDefault();
  });
})();
