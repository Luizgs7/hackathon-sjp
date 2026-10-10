/* Biblioteca independente de app.js e da API. Delegação = segura após htmx swaps. */
(() => {
  if (window.DataForgeUI) return;
  const timers = new WeakMap();
  const dialogTriggers = new WeakMap();
  const emit = (node, action, detail = {}) => node.dispatchEvent(new CustomEvent('df:action', { bubbles: true, detail: { action, id: node.id || null, ...detail } }));
  const within = event => event.target instanceof Element && event.target.closest('.df-ui');
  const validateJSON = field => {let error='';try{JSON.parse(field.value);}catch{error='JSON inválido. Revise a sintaxe.';}field.setAttribute('aria-invalid',String(Boolean(error)));const box=field.closest('.df-field');box.querySelector('[data-json-save]').disabled=Boolean(error);const help=box.querySelector('small');help.textContent=error||'JSON válido';help.classList.toggle('df-error',Boolean(error));};
  const selectTab = button => {
    const group = button.closest('[data-tabs]');
    if (!group) return;
    group.querySelectorAll('[role=tab]').forEach(tab => {
      const selected = tab === button;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      const panel = document.getElementById(tab.getAttribute('aria-controls'));
      if (panel) panel.hidden = !selected;
    });
    emit(button, 'tab', { value: button.textContent.trim() });
  };
  const theme = choice => {
    const actual = choice === 'system' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : choice;
    document.querySelectorAll('.df-ui[data-theme]').forEach(root => root.dataset.theme = actual);
    try { localStorage.setItem('dataforge.catalog.theme', choice); } catch {}
  };
  document.addEventListener('click', event => {
    if (!within(event)) return;
    const target = event.target.closest('button, [data-action]');
    if (!target || target.disabled) return;
    if (target.matches('[data-busy], [aria-disabled="true"]')) { event.preventDefault(); event.stopPropagation(); return; }
    if (target.matches('[data-toggle]')) { const group=target.closest('[data-exclusive]'); if(group) group.querySelectorAll('[data-toggle]').forEach(b=>b.setAttribute('aria-pressed',String(b===target))); else target.setAttribute('aria-pressed', String(target.getAttribute('aria-pressed') !== 'true')); emit(target, 'toggle', { value: target.getAttribute('aria-pressed') === 'true' }); }
    if (target.matches('[role=tab]')) selectTab(target);
    if (target.matches('[data-page]')) { const nav = target.closest('nav'); let page = Number(nav.dataset.current || nav.querySelector('[aria-current=page]')?.dataset.page || 1); page = target.dataset.page === 'next' ? Math.min(Number(nav.dataset.pages), page + 1) : target.dataset.page === 'previous' ? Math.max(1, page - 1) : Number(target.dataset.page); nav.dataset.current=page; nav.querySelectorAll('[data-page]').forEach(b => b.dataset.page === String(page) ? b.setAttribute('aria-current', 'page') : b.removeAttribute('aria-current')); nav.querySelector('output').value=`Página ${page}`; emit(target, 'page', { value: page }); }
    if (target.matches('[data-select]')) { const menu = target.closest('details'); menu.open = false; menu.querySelector('summary').focus(); emit(target, 'select', { value: target.dataset.select }); }
    if (target.matches('[data-dialog-open]')) { const dialog = document.getElementById(target.dataset.dialogOpen); if (dialog) { dialogTriggers.set(dialog, target); dialog.showModal(); } }
    if (target.matches('[data-dialog-close]')) target.closest('dialog')?.close('cancel');
    if (target.matches('[data-action]')) emit(target, target.dataset.action);
    if(target.matches('[data-action="json-reset"]')){const field=target.closest('.df-component').querySelector('[data-json-editor]');field.value=field.defaultValue;validateJSON(field);field.focus();}
    if(target.matches('[data-json-save]')){const field=target.closest('.df-component').querySelector('[data-json-editor]');try{emit(target,'json-save',{value:JSON.parse(field.value)});}catch{}}
    if (target.matches('[data-timer-toggle]')) { const box = target.closest('[data-timer]'); const running = timers.get(box); if (running) { clearInterval(running); timers.delete(box); target.textContent = 'Continuar'; } else { let remaining = Number(box.dataset.remaining || 300); target.textContent = 'Pausar'; const tick = setInterval(() => { if (!box.isConnected) { clearInterval(tick); return; } remaining--; box.dataset.remaining = remaining; box.querySelector('output').value = `${Math.floor(remaining / 60).toString().padStart(2, '0')}:${(remaining % 60).toString().padStart(2, '0')}`; if (remaining <= 0) { clearInterval(tick); timers.delete(box); target.textContent = 'Iniciar'; emit(box, 'timer-complete'); } }, 1000); timers.set(box, tick); } }
    if (target.matches('[data-timer-reset]')) { const box = target.closest('[data-timer]'); clearInterval(timers.get(box)); timers.delete(box); const minutes=Number(box.querySelector('[data-timer-duration]:checked')?.value || 5);box.dataset.remaining = minutes*60; box.querySelector('output').value = `${String(minutes).padStart(2,'0')}:00`; box.querySelector('[data-timer-toggle]').textContent = 'Iniciar'; }
  });
  document.addEventListener('keydown', event => {
    if (!within(event)) return;
    if (event.target.matches('[role=tab]') && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); const tabs = [...event.target.closest('[role=tablist]').querySelectorAll('[role=tab]')]; let index = tabs.indexOf(event.target); index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length; tabs[index].focus(); selectTab(tabs[index]); }
    const menu=event.target.closest('details[data-menu]');
    if(menu?.open && ['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();const choices=[...menu.querySelectorAll('button:not(:disabled)')];if(choices.length){let index=choices.indexOf(event.target);index=event.key==='Home'?0:event.key==='End'?choices.length-1:(index+(event.key==='ArrowDown'?1:-1)+choices.length)%choices.length;choices[index].focus();}}
    if (event.key === 'Escape') { const menu = event.target.closest('details[data-menu]'); if (menu) { menu.open = false; menu.querySelector('summary').focus(); } const tooltip = event.target.closest('.df-tooltip'); if (tooltip) { tooltip.querySelector('[role=tooltip]').hidden = true; tooltip.addEventListener('focusout', () => tooltip.querySelector('[role=tooltip]').hidden = false, { once: true }); } }
  });
  document.addEventListener('toggle', event => { if (event.target.matches?.('.df-ui [data-menu]') && event.target.open) event.target.querySelector('button')?.focus(); }, true);
  document.addEventListener('close', event => { const dialog = event.target; if (dialog instanceof HTMLDialogElement && dialog.closest('.df-ui')) { dialogTriggers.get(dialog)?.focus(); emit(dialog, 'dialog-close', { value: dialog.returnValue }); } }, true);
  document.addEventListener('submit', event => { if (within(event) && event.target.matches('[data-composer]')) { event.preventDefault(); const field = event.target.querySelector('textarea'); if (field.value.trim()) { emit(event.target, 'message', { value: field.value.trim() }); field.value = ''; field.focus(); } } });
  document.addEventListener('input', event => {
    if (!within(event)) return;
    if (event.target.matches('[data-range]')) { event.target.parentElement.querySelector('output').value = event.target.value; emit(event.target, 'range', { value: Number(event.target.value) }); }
    if(event.target.matches('[data-composer] textarea')){const form=event.target.closest('form');form.querySelector('[type=submit]').disabled=!event.target.value.trim() || form.getAttribute('aria-busy')==='true';}
    if(event.target.matches('[data-counted]'))event.target.closest('.df-field').querySelector('.df-character-count').value=`${event.target.value.length} caracteres`;
    if(event.target.matches('[data-json-editor]'))validateJSON(event.target);
    if (event.target.matches('[data-catalog-search]')) { const query = event.target.value.toLowerCase().trim(); let shown = 0; document.querySelectorAll('[data-search]').forEach(card => { card.hidden = !card.dataset.search.includes(query); if (!card.hidden) shown++; }); document.querySelectorAll('.df-catalog-section').forEach(section => { if (section.querySelector('[data-search]')) section.hidden = !section.querySelector('[data-search]:not([hidden])'); }); document.getElementById('search-status').textContent = `${shown} exemplos encontrados`; }
  });
  document.addEventListener('change', event => { if (!within(event)) return; if (event.target.matches('[data-theme-control]')) theme(event.target.value); else if (event.target.matches('input,select,textarea')) {if(event.target.matches('[data-indeterminate]'))event.target.removeAttribute('aria-checked');if(event.target.matches('[data-timer-duration]'))event.target.closest('[data-timer]').querySelector('[data-timer-reset]').click(); const rating=event.target.closest('.df-rating');if(rating)rating.querySelectorAll('label').forEach(label=>label.classList.toggle('df-rated',Number(label.querySelector('input').value)<=Number(event.target.value)));emit(event.target, 'change', { value: event.target.type === 'checkbox' ? event.target.checked : event.target.value });} });
  let toastTimeout;
  document.addEventListener('df:action', event => { const output = document.querySelector('[data-event-output]'); if (!output) return; output.textContent = `Evento local: ${event.detail.action}. Nenhuma chamada ao backend.`; output.hidden = false; clearTimeout(toastTimeout); toastTimeout = setTimeout(() => output.hidden = true, 3500); });
  document.addEventListener('click', event => { document.querySelectorAll('.df-ui details[data-menu][open]').forEach(menu => { if (!menu.contains(event.target)) menu.open = false; }); });
  document.addEventListener('click',event=>{if(event.target.closest?.('.df-catalog-index a')){const search=document.querySelector('[data-catalog-search]');search.value='';search.dispatchEvent(new Event('input',{bubbles:true}));}});
  const initialized=new WeakSet();
  const init = () => { document.querySelectorAll('.df-ui [data-indeterminate]').forEach(input=>{if(!initialized.has(input)){input.indeterminate=true;initialized.add(input);}});let choice = 'light'; try { choice = localStorage.getItem('dataforge.catalog.theme') || 'light'; } catch {} const control = document.querySelector('[data-theme-control]'); if (control) { control.value = choice; theme(choice); } };
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (document.querySelector('[data-theme-control]')?.value === 'system') theme('system'); });
  let savedFocus;
  document.addEventListener('htmx:beforeSwap',()=>{const node=document.activeElement;savedFocus=node?.closest?.('.df-ui')&&node.id?{node,id:node.id,start:node.selectionStart,end:node.selectionEnd}:null;});
  document.addEventListener('htmx:afterSwap',()=>{init();if(savedFocus&&!savedFocus.node.isConnected&&document.activeElement===document.body){const node=document.getElementById(savedFocus.id);node?.focus({preventScroll:true});if(node?.setSelectionRange&&savedFocus.start!==null){try{node.setSelectionRange(savedFocus.start,savedFocus.end);}catch{}}}savedFocus=null;});
  window.DataForgeUI = Object.freeze({ init, emit, theme });
  init();
})();
