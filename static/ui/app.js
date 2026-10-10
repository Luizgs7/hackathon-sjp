/* DataForge · comportamento das telas do produto. Depende de library.js e htmx. Não chama APIs próprias. */
(() => {
  if (window.DFApp) return;
  const KEY = 'dataforge.theme';
  const root = document.documentElement;
  const $ = (sel, ctx = document) => ctx.querySelector(sel);
  const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));

  /* ---------- tema: claro, escuro ou sistema; preferência persistida ---------- */
  const stored = () => { try { return localStorage.getItem(KEY) || 'system'; } catch { return 'system'; } };
  const resolve = choice => choice === 'system' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : choice;
  const applyTheme = choice => {
    const actual = resolve(choice);
    root.dataset.theme = actual;
    root.dataset.themeChoice = choice;
    $$('.df-ui').forEach(el => { el.dataset.theme = actual; });
    $$('[data-theme-choice]').forEach(btn => btn.setAttribute('aria-pressed', String(btn.dataset.themeChoice === choice)));
  };
  const setTheme = choice => { try { localStorage.setItem(KEY, choice); } catch {} applyTheme(choice); };
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (stored() === 'system') applyTheme('system'); });
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-theme-choice]');
    if (b) setTheme(b.dataset.themeChoice);
  });

  /* ---------- botões ocupados: impede envio duplicado e restaura ao terminar ---------- */
  const loader = '<span class="df-button-loader" aria-hidden="true"><svg class="df-icon" aria-hidden="true" focusable="false"><use href="#spinner"/></svg></span>';
  const setBusy = (btn, on) => {
    if (!btn) return;
    if (on) {
      if (btn.hasAttribute('data-busy')) return;
      btn.dataset.busy = '';
      btn.setAttribute('aria-busy', 'true');
      btn.setAttribute('aria-disabled', 'true');
      const w = btn.getBoundingClientRect().width;
      if (w) btn.style.minWidth = w + 'px';
      btn.insertAdjacentHTML('beforeend', loader);
    } else {
      btn.removeAttribute('data-busy');
      btn.removeAttribute('aria-busy');
      btn.removeAttribute('aria-disabled');
      btn.style.minWidth = '';
      btn.querySelector(':scope > .df-button-loader')?.remove();
    }
  };
  const submitters = form => $$('button[type=submit], button:not([type])', form);
  const busyForm = (form, on) => submitters(form).forEach(b => setBusy(b, on));

  /* ---------- erro inline de envio (mantém o que foi digitado) ---------- */
  const showFormError = (form, text) => {
    let box = $('.df-form-error', form);
    if (!box) {
      box = document.createElement('div');
      box.className = 'df-notice df-notice-danger df-form-error';
      box.setAttribute('role', 'alert');
      box.innerHTML = '<svg class="df-icon" aria-hidden="true" focusable="false"><use href="#warning"/></svg><div><span data-msg></span> <button type="button" class="df-button df-secondary df-size-small" data-retry>Tentar novamente</button></div>';
      form.prepend(box);
    }
    $('[data-msg]', box).textContent = text;
    box.setAttribute('data-show', '');
    box.querySelector('[data-retry]').onclick = () => { box.removeAttribute('data-show'); form.requestSubmit(); };
  };
  const clearFormError = form => $('.df-form-error', form)?.removeAttribute('data-show');

  /* ---------- confirmação de ações destrutivas ---------- */
  let confirmDialog;
  const ensureDialog = () => {
    if (confirmDialog) return confirmDialog;
    confirmDialog = document.createElement('dialog');
    confirmDialog.className = 'df-dialog df-modal-small';
    confirmDialog.setAttribute('aria-labelledby', 'df-confirm-title');
    confirmDialog.innerHTML = '<div class="df-row"><h3 id="df-confirm-title">Confirmar ação</h3></div><p data-confirm-text></p><form method="dialog"><button class="df-button df-secondary" value="cancel">Cancelar</button><button class="df-button df-destructive" value="confirm" data-confirm-ok>Confirmar</button></form>';
    document.body.append(confirmDialog);
    return confirmDialog;
  };
  const askConfirm = (form, trigger) => {
    const dlg = ensureDialog();
    $('[data-confirm-text]', dlg).textContent = form.dataset.confirm;
    $('[data-confirm-ok]', dlg).textContent = form.dataset.confirmLabel || 'Confirmar';
    dlg.returnValue = '';
    dlg.addEventListener('close', () => {
      if (dlg.returnValue === 'confirm') { form.dataset.confirmed = '1'; form.requestSubmit(trigger && trigger.form === form ? trigger : undefined); }
      else trigger?.focus();
    }, { once: true });
    dlg.showModal();
  };

  /* ---------- envio nativo ---------- */
  document.addEventListener('submit', e => {
    const form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.closest('.df-ui')) return;
    if (form.method === 'dialog') return;
    if (form.dataset.confirm && form.dataset.confirmed !== '1') {
      e.preventDefault();
      askConfirm(form, e.submitter);
      return;
    }
    delete form.dataset.confirmed;
    if (!form.checkValidity()) return;
    if (form.dataset.dfSubmitting === '1') { e.preventDefault(); return; }
    clearFormError(form);
    if (!form.hasAttribute('hx-post') && !form.hasAttribute('hx-get')) {
      form.dataset.dfSubmitting = '1';
      busyForm(form, true);
    }
  }, true);
  window.addEventListener('pageshow', () => { $$('form[data-df-submitting]').forEach(f => { delete f.dataset.dfSubmitting; busyForm(f, false); }); });

  /* ---------- htmx: carregamento, falha, sessão expirada, reconexão ---------- */
  let offline = null;
  const banner = () => {
    if (offline) return offline;
    offline = document.createElement('div');
    offline.className = 'df-offline';
    offline.setAttribute('role', 'status');
    offline.hidden = true;
    offline.textContent = 'Sem conexão com o servidor. Suas informações continuam na tela; tentando de novo…';
    document.body.prepend(offline);
    return offline;
  };
  const sessionNotice = () => {
    if ($('#df-session')) return;
    const n = document.createElement('div');
    n.id = 'df-session';
    n.className = 'df-notice df-notice-warn';
    n.setAttribute('role', 'alert');
    n.innerHTML = '<svg class="df-icon" aria-hidden="true" focusable="false"><use href="#lock-simple"/></svg><div><b>Sua sessão expirou.</b> <a href="/login">Entrar novamente</a></div>';
    ($('.df-toasts') || document.body).append(n);
  };
  const isPoll = el => el && el.hasAttribute('hx-trigger') && /every/.test(el.getAttribute('hx-trigger'));
  document.body.addEventListener('htmx:beforeRequest', e => {
    const el = e.detail.elt;
    if (el instanceof HTMLFormElement) { clearFormError(el); busyForm(el, true); }
    else if (el instanceof HTMLButtonElement && !isPoll(el)) setBusy(el, true);
  });
  document.body.addEventListener('htmx:afterRequest', e => {
    const el = e.detail.elt;
    if (el instanceof HTMLFormElement) busyForm(el, false);
    else if (el instanceof HTMLButtonElement) setBusy(el, false);
    if (e.detail.successful && offline) offline.hidden = true;
  });
  document.body.addEventListener('htmx:sendError', e => {
    const el = e.detail.elt;
    if (el instanceof HTMLFormElement) showFormError(el, 'Não foi possível enviar. Verifique a conexão e tente novamente.');
    else if (isPoll(el)) banner().hidden = false;
  });
  const serverDetail = xhr => {
    try { return new DOMParser().parseFromString(xhr.responseText, 'text/html').querySelector('.df-error-state p')?.textContent?.trim() || ''; } catch { return ''; }
  };
  document.body.addEventListener('htmx:responseError', e => {
    const el = e.detail.elt; const st = e.detail.xhr.status;
    if (el instanceof HTMLFormElement) {
      const detail = serverDetail(e.detail.xhr);
      const msg = st === 403 ? 'Você não tem permissão para esta ação.'
        : st === 404 ? 'O registro não foi encontrado. Atualize a página e tente novamente.'
        : st >= 500 ? 'O servidor não conseguiu concluir o pedido. Tente novamente em instantes.'
        : detail || 'Não foi possível concluir o envio. Revise os dados e tente novamente.';
      showFormError(el, msg);
    }
  });
  /* após navegação por boost, leva o foco ao conteúdo e atualiza o título */
  document.body.addEventListener('htmx:afterSettle', e => {
    if (e.detail.boosted || e.target === document.body) {
      const main = document.getElementById('conteudo');
      if (main) { main.focus({ preventScroll: true }); }
      applyTheme(stored());
    }
  });
  document.body.addEventListener('htmx:beforeSwap', e => {
    const url = e.detail.xhr && e.detail.xhr.responseURL || '';
    if (/\/login(\?|$)/.test(new URL(url, location.href).pathname + new URL(url, location.href).search) && !/\/login$/.test(location.pathname)) {
      e.detail.shouldSwap = false; sessionNotice();
    }
  });
  document.body.addEventListener('htmx:afterSwap', e => {
    const t = e.detail.target;
    $$('.df-chat-log', t).forEach(l => { l.scrollTop = l.scrollHeight; });
    if (t.matches?.('.df-chat-log')) t.scrollTop = t.scrollHeight;
    window.DataForgeUI?.init();
  });

  /* ---------- coluna de lista como gaveta em telas estreitas ---------- */
  const listCol = () => document.getElementById('df-list');
  const setList = open => {
    const col = listCol(); if (!col) return;
    col.classList.toggle('is-open', open);
    const scrim = $('[data-list-scrim]'); if (scrim) scrim.hidden = !open;
    $$('[data-list-toggle]').forEach(b => b.setAttribute('aria-expanded', String(open)));
    if (open) { (col.querySelector('a, button, input') || col).focus?.(); }
    else $('[data-list-toggle]')?.focus();
  };
  document.addEventListener('click', e => {
    if (e.target.closest('[data-list-toggle]')) { setList(!listCol()?.classList.contains('is-open')); return; }
    if (e.target.closest('[data-list-scrim]') || (e.target.closest('#df-list a') && listCol()?.classList.contains('is-open'))) setList(false);
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && listCol()?.classList.contains('is-open')) setList(false); });

  /* ---------- filtros que enviam ao mudar, avaliação por estrelas e JSON ---------- */
  document.addEventListener('change', e => {
    const f = e.target.closest?.('form[data-autosubmit]');
    if (f) f.requestSubmit();
    const rating = e.target.closest?.('.df-rating');
    if (rating && e.target.matches('input[type=radio]')) paintRating(rating);
  });
  const paintRating = box => {
    const checked = box.querySelector('input:checked');
    const v = checked ? Number(checked.value) : 0;
    $$('label', box).forEach(l => { const i = l.querySelector('input'); l.classList.toggle('df-rated', Number(i.value) <= v); });
  };
  const initWidgets = () => {
    $$('.df-rating').forEach(paintRating);
    $$('textarea[data-json]').forEach(validateJson);
  };
  const validateJson = field => {
    let err = '';
    try { JSON.parse(field.value); } catch { err = 'JSON inválido. Revise a sintaxe antes de salvar.'; }
    field.setAttribute('aria-invalid', String(Boolean(err)));
    const help = document.getElementById(field.getAttribute('aria-describedby'));
    if (help) { help.textContent = err || 'JSON válido'; help.classList.toggle('df-error', Boolean(err)); }
    const form = field.form;
    if (form) $$('button[type=submit], button:not([type])', form).forEach(b => { b.disabled = Boolean(err); });
  };
  document.addEventListener('input', e => { if (e.target.matches?.('textarea[data-json]')) validateJson(e.target); });
  document.addEventListener('DOMContentLoaded', initWidgets);
  document.body.addEventListener('htmx:afterSettle', initWidgets);


  /* ---------- busca local em listas laterais ---------- */
  document.addEventListener('input', e => {
    const inp = e.target.closest?.('[data-filter-list]'); if (!inp) return;
    const q = inp.value.toLowerCase().trim();
    $$('[data-filter-text]', document.querySelector(inp.dataset.filterList)).forEach(a => { a.hidden = q && !a.dataset.filterText.toLowerCase().includes(q); });
  });

  /* marca o item atual em listas laterais carregadas por htmx */
  const markCurrent = () => $$('#df-list a[href]').forEach(a => { if (a.getAttribute('href') === location.pathname) a.setAttribute('aria-current', 'page'); });
  document.body.addEventListener('htmx:afterSwap', markCurrent);
  document.addEventListener('DOMContentLoaded', markCurrent);
  /* ---------- rolagem de conversa e campos ---------- */
  const toEnd = () => $$('.df-chat-log').forEach(l => { l.scrollTop = l.scrollHeight; });
  document.addEventListener('DOMContentLoaded', () => { applyTheme(stored()); toEnd(); });
  applyTheme(stored());

  /* ---------- toasts a partir do parâmetro ?msg ---------- */
  window.DFApp = Object.freeze({ setTheme, setBusy, toEnd, showFormError });
})();

/* Abas (WAI-ARIA): setas, Home/End, estado lembrado no hash */
(() => {
  const ativar = (lista, tab, foco) => {
    lista.querySelectorAll('[role="tab"]').forEach(t => {
      const on = t === tab;
      t.setAttribute('aria-selected', on);
      t.tabIndex = on ? 0 : -1;
      const p = document.getElementById(t.getAttribute('aria-controls'));
      if (p) p.hidden = !on;
    });
    if (foco) tab.focus();
  };
  const iniciar = () => document.querySelectorAll('[data-tabs]:not([data-tabs-ok])').forEach(box => {
    box.dataset.tabsOk = '1';
    const lista = box.querySelector('[role="tablist"]');
    const tabs = [...lista.querySelectorAll('[role="tab"]')];
    let salvo = null; try { salvo = box.dataset.tabsKey ? sessionStorage.getItem('df.tab.' + box.dataset.tabsKey) : null; } catch (e) {}
    const inicial = tabs.find(t => t.id === salvo) || tabs.find(t => '#' + t.id === location.hash) || tabs[0];
    ativar(lista, inicial, false);
    lista.addEventListener('click', e => { const t = e.target.closest('[role="tab"]'); if (t) { ativar(lista, t, false); try { if (box.dataset.tabsKey) sessionStorage.setItem('df.tab.' + box.dataset.tabsKey, t.id); } catch (e2) {} } });
    lista.addEventListener('keydown', e => {
      const i = tabs.indexOf(document.activeElement);
      if (i < 0) return;
      const n = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
      if (n === undefined) return;
      e.preventDefault();
      ativar(lista, tabs[(n + tabs.length) % tabs.length], true);
    });
  });
  document.addEventListener('DOMContentLoaded', iniciar);
  document.body && document.body.addEventListener('htmx:afterSettle', iniciar);
  document.addEventListener('htmx:afterSettle', iniciar);
})();

/* Contador de caracteres: textarea[data-counter="#id"] */
(() => {
  const atualizar = t => { const o = document.querySelector(t.dataset.counter); if (o) o.textContent = t.value.length + '/' + (t.maxLength > 0 ? t.maxLength : '∞'); };
  document.addEventListener('input', e => { if (e.target.matches && e.target.matches('textarea[data-counter]')) atualizar(e.target); });
  const iniciar = () => document.querySelectorAll('textarea[data-counter]').forEach(atualizar);
  document.addEventListener('DOMContentLoaded', iniciar);
  document.addEventListener('htmx:afterSettle', iniciar);
})();

/* Quadro kanban: botões Anterior/Próxima levam de coluna em coluna; a posição sobrevive às atualizações do htmx. */
(() => {
  const quadroDe = nav => { let x = nav.nextElementSibling; while (x && !x.classList.contains('df-board')) x = x.nextElementSibling; return x; };  // a barra superior fica entre o nav e o quadro
  const cols = b => [...b.querySelectorAll('.df-board-col')];
  const atual = b => { const x = b.scrollLeft + 4; let i = 0; cols(b).forEach((c, k) => { if (c.offsetLeft - b.offsetLeft <= x) i = k; }); return i; };
  const sincronizar = () => document.querySelectorAll('[data-board-nav]').forEach(nav => {
    const b = quadroDe(nav); if (!b) return;
    const n = cols(b).length, i = atual(b), fim = b.scrollLeft + b.clientWidth >= b.scrollWidth - 4;
    nav.querySelector('[data-board-dir="-1"]').disabled = b.scrollLeft <= 0;
    nav.querySelector('[data-board-dir="1"]').disabled = fim;
    const nome = cols(b)[i] && cols(b)[i].querySelector('header span');
    nav.querySelector('[data-board-pos]').textContent = nome ? nome.textContent + ' · ' + (i + 1) + ' de ' + n : '';
    if (!b.dataset.navBound) { b.dataset.navBound = '1'; b.addEventListener('scroll', sincronizar, { passive: true }); }
  });
  document.addEventListener('click', e => {
    const bt = e.target.closest && e.target.closest('[data-board-dir]'); if (!bt) return;
    const b = quadroDe(bt.closest('[data-board-nav]')); if (!b) return;
    const cs = cols(b);
    const alvo = Math.min(Math.max(atual(b) + Number(bt.dataset.boardDir), 0), cs.length - 1);
    b.scrollTo({ left: cs[alvo].offsetLeft - b.offsetLeft, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  });
  let guardado = 0;
  document.addEventListener('htmx:beforeSwap', e => { const b = e.detail.target && e.detail.target.querySelector && e.detail.target.querySelector('.df-board'); if (b) guardado = b.scrollLeft; });
  document.addEventListener('htmx:afterSettle', () => { const b = document.querySelector('.df-board'); if (b && guardado) { b.scrollLeft = guardado; } sincronizar(); });
  document.addEventListener('DOMContentLoaded', sincronizar);
  window.addEventListener('resize', sincronizar);
})();


/* Imagem de evidência que não carrega (arquivo ausente em outra instância): troca por espaço reservado */
document.addEventListener('error', e => {
  const img = e.target;
  if (!img || img.tagName !== 'IMG' || !img.hasAttribute('data-foto')) return;
  const box = document.createElement('div');
  box.className = 'df-empty';
  box.innerHTML = '<svg class="df-icon" aria-hidden="true" focusable="false"><use href="#image"/></svg><p></p>';
  box.querySelector('p').textContent = img.dataset.vazio || 'Imagem indisponível.';
  img.replaceWith(box);
}, true);


/* Kanban: barra de rolagem superior espelha o scroll horizontal do quadro */
(() => {
  const ligar = () => document.querySelectorAll('[data-board-scrolltop]').forEach(topo => {
    const b = topo.nextElementSibling; if (!b || !b.classList.contains('df-board')) return;
    topo.firstElementChild.style.width = b.scrollWidth + 'px';
    if (topo.dataset.ligado) return; topo.dataset.ligado = '1';
    let trava = false;
    topo.addEventListener('scroll', () => { if (trava) { trava = false; return; } trava = true; b.scrollLeft = topo.scrollLeft; }, { passive: true });
    b.addEventListener('scroll', () => { if (trava) { trava = false; return; } trava = true; topo.scrollLeft = b.scrollLeft; }, { passive: true });
  });
  document.addEventListener('DOMContentLoaded', ligar);
  document.addEventListener('htmx:afterSettle', () => { ligar(); const t = document.querySelector('[data-board-scrolltop]'), b = document.querySelector('.df-board'); if (t && b) t.scrollLeft = b.scrollLeft; });
  window.addEventListener('resize', ligar);
})();
