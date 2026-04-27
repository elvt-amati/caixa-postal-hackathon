// Caixa Postal — SPA with sidebar, per-agent chats, card views, context bridge, generative chart.
// AG-UI SSE consumer written in vanilla JS (no CopilotKit dependency).

// ============================================================================
// State
// ============================================================================

const state = {
  view: { kind: 'home' },
  me: { user: { id: 'anon', email: '', name: 'Anônimo' }, auth_enabled: false, authenticated: false },
  agents: {},
  threads: {},
  cards: ['task','payment','reminder','contact','note','email','pitch'],
  cardData: {},
  pinnedCards: loadPinned(),
  homeLayout: loadHomeLayout(),              // [{agent, key}] — Phase C
  snapshots: loadSnapshots(),                // {agent: {key: {template, spec, updated_at}}} — Phase D
  editingHome: false,
  calendar: { cursor: new Date(), range: 'month', showChart: false },
  recording: null,
  pendingImage: null,
  bridgeContext: null,
};

const CAT_META = {
  task:     { label: 'Tarefas',    emoji: '✅', color: 'var(--blue)' },
  payment:  { label: 'Pagamentos', emoji: '💰', color: 'var(--orange)' },
  reminder: { label: 'Lembretes',  emoji: '⏰', color: 'var(--purple)' },
  contact:  { label: 'Contatos',   emoji: '👥', color: 'var(--green)' },
  note:     { label: 'Notas',      emoji: '📝', color: 'var(--note)' },
  email:    { label: 'Emails',     emoji: '✉️', color: 'var(--red)' },
  pitch:    { label: 'Pitches',    emoji: '🎯', color: 'var(--accent)' },
};

function loadPinned() {
  try { return JSON.parse(localStorage.getItem('caixa.pinned') || '["task","payment","reminder"]'); }
  catch { return ['task','payment','reminder']; }
}
function savePinned() { localStorage.setItem('caixa.pinned', JSON.stringify(state.pinnedCards)); }

function loadHomeLayout() {
  try {
    const raw = localStorage.getItem('caixa.homeLayout');
    if (raw) return JSON.parse(raw);
  } catch {}
  return null; // computed on first render from pinnedCards
}
function saveHomeLayout() { localStorage.setItem('caixa.homeLayout', JSON.stringify(state.homeLayout || [])); }

function loadSnapshots() {
  try { return JSON.parse(localStorage.getItem('caixa.snapshots') || '{}'); }
  catch { return {}; }
}
function saveSnapshots() { localStorage.setItem('caixa.snapshots', JSON.stringify(state.snapshots || {})); }

// ============================================================================
// DOM helpers
// ============================================================================

const $  = (id) => document.getElementById(id);
const $$ = (sel, root=document) => [...root.querySelectorAll(sel)];
function esc(s){ const d=document.createElement('div'); d.textContent=String(s==null?'':s); return d.innerHTML; }
function uid(){ return 'm-' + Math.random().toString(36).slice(2,10); }
function now(){ return new Date().toTimeString().slice(0,5); }
function tmpl(id){ return $(id).content.firstElementChild.cloneNode(true); }

// ============================================================================
// Router
// ============================================================================

function navigate(view) {
  // Cleanup transient state that shouldn't leak across views:
  // - stop any in-flight MediaRecorder so mic is released (P0.12)
  // - reset edit-home mode so pinned-× overlays don't linger (P0.13)
  try {
    if (state.recording) {
      state.recording.stop();
      state.recording = null;
    }
  } catch {}
  if (state.view && state.view.kind !== view.kind) {
    state.editingHome = false;
  }
  state.view = view;
  renderMain();
  renderSidebar();
}

function renderMain() {
  const main = $('main');
  main.innerHTML = '';
  const v = state.view;
  if (v.kind === 'home')  main.appendChild(renderHome());
  else if (v.kind === 'chat')  main.appendChild(renderChat(v.agent));
  else if (v.kind === 'card')  main.appendChild(renderCard(v.cat));
}

function renderSidebar() {
  renderUserPill();
  // Mark active nav-item
  $$('.nav-item').forEach(n => n.classList.remove('active'));
  if (state.view.kind === 'home') $$('.nav-item[data-view="home"]')[0].classList.add('active');

  // Agents list
  const al = $('agentsList');
  al.innerHTML = '';
  const agents = [['concierge', {display_name:'Concierge', emoji:'🤖', description:'Orquestrador'}], ...Object.entries(chatAgents())];
  for (const [name, meta] of agents) {
    const el = document.createElement('button');
    el.className = 'nav-item';
    if (state.view.kind === 'chat' && state.view.agent === name) el.classList.add('active');
    el.dataset.agent = name;
    el.innerHTML = `<span class="ic">${meta.emoji || '🤖'}</span><span class="lbl">${esc(meta.display_name || name)}</span>`;
    el.onclick = () => navigate({ kind: 'chat', agent: name });
    al.appendChild(el);
  }

  // Cards list
  const cl = $('cardsList');
  cl.innerHTML = '';
  for (const cat of state.cards) {
    const meta = CAT_META[cat];
    const count = (state.cardData[cat] || []).length;
    const el = document.createElement('button');
    el.className = 'nav-item';
    if (state.view.kind === 'card' && state.view.cat === cat) el.classList.add('active');
    el.innerHTML = `<span class="ic">${meta.emoji}</span><span class="lbl">${meta.label}</span><span class="count">${count}</span>`;
    el.onclick = () => navigate({ kind: 'card', cat });
    cl.appendChild(el);
  }
}

// ============================================================================
// Home view
// ============================================================================

function renderHome() {
  const el = tmpl('tpl-home');
  // Add edit-mode toggle to header
  const act = el.querySelector('.view-actions');
  const editBtn = document.createElement('button');
  editBtn.className = 'btn' + (state.editingHome ? ' primary' : '');
  editBtn.textContent = state.editingHome ? '✓ pronto' : '✎ editar home';
  editBtn.onclick = () => { state.editingHome = !state.editingHome; renderMain(); };
  act.insertBefore(editBtn, act.firstChild);

  el.querySelector('#refreshAll').onclick = refreshAll;
  el.querySelector('#resetAll').onclick = async () => {
    if (!confirm('Apagar TODOS os itens?')) return;
    await fetch('/api/ops/reset', {method:'POST'});
    refreshAll();
  };
  if (state.editingHome) el.classList.add('home-edit');

  // Ensure homeLayout is hydrated — if null, derive default from legacy pinnedCards
  if (!state.homeLayout) {
    state.homeLayout = state.pinnedCards.map(cat => ({ agent: '__core__', key: cat }));
    saveHomeLayout();
  }

  // Palette of candidates when editing
  if (state.editingHome) {
    const palette = renderEditPalette();
    el.insertBefore(palette, el.querySelector('#homeGrid'));
  }

  renderHomeGrid(el.querySelector('#homeGrid'));
  return el;
}

function allCatalogCards() {
  // Returns flat [{agent, key, template, title, source, spec?}] from agent catalogs + snapshots
  const out = [];
  for (const [agent, meta] of Object.entries(state.agents)) {
    for (const c of (meta.cards || [])) out.push({ agent, ...c });
  }
  for (const [agent, keys] of Object.entries(state.snapshots || {})) {
    for (const [key, snap] of Object.entries(keys)) {
      out.push({ agent, key, template: snap.template || 'list', title: snap.title || `${agent}/${key}`, source: null, isSnapshot: true, snapshot: snap });
    }
  }
  return out;
}

function layoutHas(agent, key) {
  return (state.homeLayout || []).some(l => l.agent === agent && l.key === key);
}
function layoutAdd(agent, key) {
  state.homeLayout = state.homeLayout || [];
  if (!layoutHas(agent, key)) { state.homeLayout.push({ agent, key }); saveHomeLayout(); }
}
function layoutRemove(agent, key) {
  state.homeLayout = (state.homeLayout || []).filter(l => !(l.agent === agent && l.key === key));
  saveHomeLayout();
}

function renderEditPalette() {
  const wrap = document.createElement('div');
  wrap.className = 'palette';
  wrap.innerHTML = '<h4>✎ adicione um card (agrupado por agente)</h4>';
  const all = allCatalogCards();
  const byAgent = {};
  for (const c of all) {
    if (layoutHas(c.agent, c.key)) continue;
    (byAgent[c.agent] = byAgent[c.agent] || []).push(c);
  }
  for (const [agent, cards] of Object.entries(byAgent)) {
    const meta = state.agents[agent] || { display_name: agent, emoji: '🤖' };
    const grp = document.createElement('div');
    grp.className = 'palette-grp';
    grp.innerHTML = `<div style="color:var(--sub);font-size:11px;width:100%;margin-bottom:4px">${meta.emoji} ${esc(meta.display_name || agent)}</div>`;
    for (const c of cards) {
      const chip = document.createElement('span');
      chip.className = 'palette-chip';
      chip.innerHTML = `<span>${templateIcon(c.template)}</span>${esc(c.title || c.key)}`;
      chip.onclick = () => { layoutAdd(c.agent, c.key); renderMain(); };
      grp.appendChild(chip);
    }
    wrap.appendChild(grp);
  }
  if (!Object.keys(byAgent).length) {
    const empty = document.createElement('div');
    empty.style = 'color:var(--sub);font-size:12px;padding:4px 0';
    empty.textContent = 'todos os cards disponíveis já estão na home. Peça a um agente "coloca X no home" pra criar cards novos via AG-UI.';
    wrap.appendChild(empty);
  }
  return wrap;
}

function templateIcon(t) {
  return { list: '📋', kanban: '📊', calendar: '📅', chart: '📈', metric: '🔢' }[t] || '🗂';
}

function findCatalogCard(agent, key) {
  // Agent-declared
  const ag = state.agents[agent];
  if (ag && ag.cards) {
    const hit = ag.cards.find(c => c.key === key);
    if (hit) return { agent, ...hit };
  }
  // Snapshot
  const snap = (state.snapshots || {})[agent] && state.snapshots[agent][key];
  if (snap) return { agent, key, template: snap.template || 'list', title: snap.title || `${agent}/${key}`, isSnapshot: true, snapshot: snap };
  return null;
}

function itemsForSource(source) {
  if (!source) return [];
  const cat = source.cat;
  let items = state.cardData[cat] || [];
  if (source.filter === 'today') {
    const today = new Date().toISOString().slice(0,10);
    items = items.filter(it => (it.at || it.due_date || '').startsWith(today));
  } else if (source.filter === 'this_week') {
    const now = new Date(); now.setHours(0,0,0,0);
    const end = new Date(now); end.setDate(end.getDate() + 7);
    items = items.filter(it => {
      const v = it.due_date || it.at;
      if (!v) return false;
      const d = new Date(v);
      return d >= now && d <= end;
    });
  }
  return items;
}

function renderHomeGrid(grid) {
  grid.innerHTML = '';
  const layout = (state.homeLayout || []).slice();
  if (!layout.length) {
    grid.innerHTML = '<div style="padding:40px;color:var(--sub);text-align:center;grid-column:1/-1">Home vazia — clica em <b>✎ editar home</b> pra adicionar cards.</div>';
    return;
  }
  for (const slot of layout) {
    const card = findCatalogCard(slot.agent, slot.key);
    if (!card) continue; // tolerant to stale layout entries
    grid.appendChild(renderCardOnHome(card));
  }
}

function renderCardOnHome(card) {
  const tpl = card.template || 'list';
  const box = document.createElement('div');
  box.className = 'home-card c-' + (card.source?.cat || 'generic');
  if (!state.editingHome) box.style.cursor = 'pointer';

  const meta = CAT_META[card.source?.cat] || { emoji: templateIcon(tpl), label: card.title || card.key };
  const items = card.isSnapshot ? [] : itemsForSource(card.source);
  const count = items.length;

  const agentMeta = state.agents[card.agent] || { display_name: card.agent, emoji: '🤖' };
  const ribbon = card.agent !== '__core__'
    ? `<span style="color:var(--sub);font-size:11px;margin-left:auto;margin-right:8px">${agentMeta.emoji||''} ${esc(agentMeta.display_name||card.agent)}</span>`
    : '';

  const removeBtn = state.editingHome
    ? `<button class="remove-card" title="remover da home" onclick="event.stopPropagation(); layoutRemoveAndRerender('${esc(card.agent)}','${esc(card.key)}')">×</button>`
    : '';

  box.innerHTML = `${removeBtn}<div class="hc-hdr">
      <span>${meta.emoji || templateIcon(tpl)} ${esc(card.title || meta.label || card.key)}</span>
      ${ribbon}
      ${!card.isSnapshot ? `<span class="hc-count">${count}</span>` : ''}
    </div><div class="hc-body" id="hc-${card.agent}-${card.key}"></div>`;

  // click to expand — only for non-snapshot, only when not editing
  if (!state.editingHome && card.source?.cat) {
    box.onclick = () => navigate({ kind: 'card', cat: card.source.cat });
  }

  // Mount template body
  setTimeout(() => {
    const body = box.querySelector('.hc-body');
    if (!body) return;
    if (card.isSnapshot) {
      mountTemplate(body, tpl, card.snapshot.spec || {}, []);
    } else {
      mountTemplate(body, tpl, card, items);
    }
  }, 0);

  return box;
}

window.layoutRemoveAndRerender = function(agent, key) {
  layoutRemove(agent, key);
  renderMain();
};

// ============================================================================
// Template renderers — each takes (container, spec, items)
// ============================================================================

function mountTemplate(container, tpl, spec, items) {
  try {
    if (tpl === 'list')     return renderTpl_list(container, spec, items);
    if (tpl === 'kanban')   return renderTpl_kanban(container, spec, items);
    if (tpl === 'calendar') return renderTpl_calendar(container, spec, items);
    if (tpl === 'chart')    return renderTpl_chart(container, spec, items);
    if (tpl === 'metric')   return renderTpl_metric(container, spec, items);
    if (tpl === 'table')    return renderTpl_table(container, spec, items);
    if (tpl === 'timeline') return renderTpl_timeline(container, spec, items);
    if (tpl === 'progress') return renderTpl_progress(container, spec, items);
    container.innerHTML = `<div class="empty">template desconhecido: ${esc(tpl)}</div>`;
  } catch (e) {
    container.innerHTML = `<div class="empty">erro renderizando: ${esc(e.message)}</div>`;
  }
}

// Template: table — spec has {columns:[...], rows:[{col1:val,...}]} OR aggregates from items via spec.source.columns.
function renderTpl_table(container, spec, items) {
  let cols = (spec.spec && spec.spec.columns) || spec.columns;
  let rows = (spec.spec && spec.spec.rows) || spec.rows;
  if (!rows && spec.source) {
    cols = spec.source.columns || Object.keys(items[0] || {}).slice(0, 5);
    rows = items.map(it => Object.fromEntries(cols.map(c => [c, it[c]])));
  }
  cols = cols || [];
  rows = rows || [];
  container.innerHTML = '';
  if (!rows.length) { container.innerHTML = '<div class="empty">sem dados</div>'; return; }
  const table = document.createElement('table');
  table.style.width = '100%';
  table.style.fontSize = '12px';
  table.style.borderCollapse = 'collapse';
  const thead = document.createElement('thead');
  thead.innerHTML = `<tr>${cols.map(c=>`<th style="text-align:left;color:var(--sub);padding:6px 8px;border-bottom:1px solid var(--stroke);text-transform:uppercase;letter-spacing:.04em;font-size:10px">${esc(c)}</th>`).join('')}</tr>`;
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  for (const r of rows.slice(0, 8)) {
    const tr = document.createElement('tr');
    tr.innerHTML = cols.map(c => `<td style="padding:6px 8px;border-bottom:1px solid var(--stroke)">${esc(r[c] ?? '')}</td>`).join('');
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  container.appendChild(table);
  if (rows.length > 8) {
    const more = document.createElement('div');
    more.style = 'color:var(--sub);font-size:11px;padding:4px 0;text-align:center';
    more.textContent = `+ ${rows.length - 8} linhas`;
    container.appendChild(more);
  }
}

// Template: timeline — spec has {events:[{date,title,sub?}]} OR pulls from items by date field.
function renderTpl_timeline(container, spec, items) {
  let events = (spec.spec && spec.spec.events) || spec.events;
  if (!events && spec.source) {
    const dateField = spec.source.date_field || 'due_date';
    const titleField = spec.source.title_field || (spec.source.cat === 'payment' ? 'description' : 'title');
    events = items
      .filter(it => it[dateField])
      .map(it => ({
        date: it[dateField],
        title: it[titleField] || '—',
        sub: it.payee || it.notes || '',
        _item: it,
      }))
      .sort((a,b) => new Date(a.date) - new Date(b.date));
  }
  events = events || [];
  container.innerHTML = '';
  if (!events.length) { container.innerHTML = '<div class="empty">nada na linha do tempo</div>'; return; }
  const wrap = document.createElement('div');
  wrap.style = 'position:relative;padding-left:20px;padding-top:4px';
  const line = document.createElement('div');
  line.style = 'position:absolute;left:6px;top:4px;bottom:4px;width:2px;background:var(--stroke)';
  wrap.appendChild(line);
  for (const ev of events.slice(0, 6)) {
    const row = document.createElement('div');
    row.style = 'position:relative;padding:6px 0 10px;font-size:13px';
    row.innerHTML = `
      <div style="position:absolute;left:-18px;top:10px;width:10px;height:10px;border-radius:999px;background:var(--accent);border:2px solid var(--bg)"></div>
      <div style="color:var(--sub);font-size:11px">${esc(ev.date)}</div>
      <div style="font-weight:500">${esc(ev.title)}</div>
      ${ev.sub ? `<div style="color:var(--sub);font-size:11px">${esc(ev.sub)}</div>` : ''}
    `;
    wrap.appendChild(row);
  }
  container.appendChild(wrap);
  if (events.length > 6) {
    const more = document.createElement('div');
    more.style = 'color:var(--sub);font-size:11px;padding:4px 0;text-align:center';
    more.textContent = `+ ${events.length - 6} eventos`;
    container.appendChild(more);
  }
}

// Template: progress — spec has {value:0..100, label, sub, target?}.
function renderTpl_progress(container, spec, items) {
  const inner = (spec.spec) || spec;
  let value = inner.value;
  const target = inner.target;
  const label = inner.label || spec.title || '';
  let sub = inner.sub || '';
  if (value == null && spec.source && target) {
    const field = spec.source.field || 'amount';
    const sum = items.reduce((a,it)=>a+Number(it[field]||0), 0);
    value = Math.min(100, Math.round((sum / target) * 100));
    sub = sub || `${sum.toFixed(2)} / ${target}`;
  }
  value = Math.max(0, Math.min(100, Number(value || 0)));
  container.innerHTML = `
    <div style="padding:6px 0 4px">
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px">
        <span style="font-weight:500;font-size:13px">${esc(label)}</span>
        <span style="color:var(--accent);font-weight:700;font-size:18px">${value}%</span>
      </div>
      <div style="height:10px;background:var(--bg);border-radius:999px;overflow:hidden;border:1px solid var(--stroke)">
        <div style="width:${value}%;height:100%;background:linear-gradient(90deg,var(--accent),var(--blue));transition:width .3s"></div>
      </div>
      ${sub ? `<div style="color:var(--sub);font-size:11px;margin-top:4px">${esc(sub)}</div>` : ''}
    </div>`;
}

function renderTpl_list(container, spec, items) {
  if (!items.length) {
    container.innerHTML = `<div class="empty">nada aqui ainda</div>`;
    return;
  }
  const cat = spec.source?.cat;
  container.innerHTML = '';
  for (const it of items.slice(0, 5)) {
    const row = document.createElement('div');
    row.className = 'item';
    row.innerHTML = renderCardPreview(cat, it);
    container.appendChild(row);
  }
  if (items.length > 5) {
    const more = document.createElement('div');
    more.style = 'color:var(--sub);font-size:11px;padding:4px 0;text-align:center';
    more.textContent = `+ ${items.length - 5} mais`;
    container.appendChild(more);
  }
}

function renderTpl_kanban(container, spec, items) {
  container.innerHTML = '';
  // Compact kanban preview: just counts per column
  const cols = { open: 0, doing: 0, done: 0 };
  for (const it of items) cols[kanbanColFor(it.status)]++;
  const row = document.createElement('div');
  row.style = 'display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding:8px 0';
  row.innerHTML = `
    <div style="background:var(--bg);border:1px solid var(--stroke);border-radius:8px;padding:10px;text-align:center"><div style="color:var(--blue);font-size:22px;font-weight:700">${cols.open}</div><div style="color:var(--sub);font-size:11px;text-transform:uppercase;letter-spacing:.04em">A fazer</div></div>
    <div style="background:var(--bg);border:1px solid var(--stroke);border-radius:8px;padding:10px;text-align:center"><div style="color:var(--orange);font-size:22px;font-weight:700">${cols.doing}</div><div style="color:var(--sub);font-size:11px;text-transform:uppercase;letter-spacing:.04em">Em andamento</div></div>
    <div style="background:var(--bg);border:1px solid var(--stroke);border-radius:8px;padding:10px;text-align:center"><div style="color:var(--green);font-size:22px;font-weight:700">${cols.done}</div><div style="color:var(--sub);font-size:11px;text-transform:uppercase;letter-spacing:.04em">Feito</div></div>
  `;
  container.appendChild(row);
}

function renderTpl_calendar(container, spec, items) {
  // Compact: next 5 due items listed
  container.innerHTML = '';
  const upcoming = items
    .filter(it => it.due_date)
    .map(it => ({...it, _d: new Date(it.due_date + 'T12:00:00')}))
    .sort((a,b) => a._d - b._d)
    .slice(0, 4);
  if (!upcoming.length) { container.innerHTML = '<div class="empty">nenhuma data registrada</div>'; return; }
  for (const it of upcoming) {
    const row = document.createElement('div');
    row.className = 'item';
    row.innerHTML = `<div class="ttl">${esc(it.description || it.title || '—')} ${it.amount?`<span style="color:var(--orange)">R$ ${Number(it.amount).toFixed(2)}</span>`:''}</div><div class="sub">📅 ${esc(it.due_date)}${it.payee?' • '+esc(it.payee):''}</div>`;
    container.appendChild(row);
  }
}

function renderTpl_chart(container, spec, items) {
  container.innerHTML = '';
  const canvas = document.createElement('canvas');
  canvas.height = 160;
  container.appendChild(canvas);
  // spec either has labels/values directly, or source with aggregate
  let chartSpec = spec.spec || spec;
  if (!chartSpec.labels && spec.source) {
    const field = spec.source.field || 'amount';
    const groupBy = spec.source.group_by || 'payee';
    const totals = {};
    for (const it of items) {
      const k = it[groupBy] || it.description || 'sem';
      totals[k] = (totals[k] || 0) + Number(it[field] || 0);
    }
    chartSpec = { chart_type: 'bar', title: '', labels: Object.keys(totals), values: Object.values(totals), unit: spec.unit || '' };
  }
  // Draw immediately if Chart.js is loaded; otherwise defer until it is
  if (window.Chart) {
    renderChartOn(canvas, chartSpec);
  } else {
    loadChartJs().then(() => renderChartOn(canvas, chartSpec));
  }
}

function renderTpl_metric(container, spec, items) {
  container.innerHTML = '';
  let value = spec.value;
  let sub = spec.sub || '';
  if (value == null && spec.source) {
    const field = spec.source.field || 'amount';
    const agg = spec.source.aggregate || 'count';
    if (agg === 'sum')   value = items.reduce((a,it)=>a+Number(it[field]||0), 0);
    else if (agg === 'avg') value = items.length ? items.reduce((a,it)=>a+Number(it[field]||0), 0) / items.length : 0;
    else value = items.length;
    sub = `${items.length} itens`;
  }
  const unit = spec.unit || '';
  const formatted = typeof value === 'number' ? (value % 1 === 0 ? value.toFixed(0) : value.toFixed(2)) : value;
  container.innerHTML = `
    <div style="padding:20px 0;text-align:center">
      <div style="font-size:34px;font-weight:700;color:var(--accent)">${esc(unit + formatted)}</div>
      <div style="color:var(--sub);font-size:12px;margin-top:4px">${esc(sub)}</div>
    </div>`;
}

function renderCardPreview(cat, it) {
  switch(cat) {
    case 'task':     return `<div class="item"><div class="ttl">${esc(it.title)}</div>${it.due_date?`<div class="sub">📅 ${esc(it.due_date)}</div>`:''}</div>`;
    case 'payment':  return `<div class="item"><div class="ttl">${esc(it.description)} <span style="color:var(--orange)">R$ ${Number(it.amount||0).toFixed(2)}</span></div>${it.due_date?`<div class="sub">📅 ${esc(it.due_date)}${it.payee?' • '+esc(it.payee):''}</div>`:''}</div>`;
    case 'reminder': return `<div class="item"><div class="ttl">${esc(it.title)}</div><div class="sub">📅 ${esc(it.at)}</div></div>`;
    case 'contact':  return `<div class="item"><div class="ttl">${esc(it.name)}</div><div class="sub">${it.phone?'📞 '+esc(it.phone)+' ':''}${it.email?'✉️ '+esc(it.email):''}</div></div>`;
    case 'note':     return `<div class="item"><div class="ttl">${esc(it.title)}</div><div class="sub">${esc((it.body||'').slice(0,80))}</div></div>`;
    case 'email':    return `<div class="item"><div class="ttl">→ ${esc(it.to)}</div><div class="sub">${esc(it.subject)}</div></div>`;
    case 'pitch':    return `<div class="item"><div class="ttl">🎯 ${esc(it.title)}</div><div class="sub">${esc(it.tagline||'')}</div></div>`;
    default: return `<div class="item">${esc(JSON.stringify(it).slice(0,80))}</div>`;
  }
}

// ============================================================================
// Card detail view
// ============================================================================

function renderCard(cat) {
  const el = tmpl('tpl-card');
  const meta = CAT_META[cat];
  el.querySelector('#cardTitle').textContent = `${meta.emoji} ${meta.label}`;
  el.querySelector('#cardSub').textContent = (state.cardData[cat] || []).length + ' itens';
  el.querySelector('#backHome').onclick = () => navigate({kind:'home'});
  el.querySelector('#cardRefresh').onclick = async () => { await refreshAll(); renderMain(); };
  const body = el.querySelector('#cardBody');
  switch(cat) {
    case 'task':     body.appendChild(renderKanban(state.cardData[cat] || [])); break;
    case 'payment':  body.appendChild(renderCalendar(state.cardData[cat] || [])); body.appendChild(renderPaymentsChart(state.cardData[cat] || [])); break;
    default:         body.appendChild(renderList(cat, state.cardData[cat] || [])); break;
  }
  return el;
}

function kanbanColFor(status) {
  const st = (status || 'open').toLowerCase();
  if (st.includes('done') || st.includes('conclu') || st.includes('feita')) return 'done';
  if (st.includes('doing') || st.includes('prog') || st.includes('andamento')) return 'doing';
  return 'open';
}

const COL_TO_STATUS = { open: 'open', doing: 'doing', done: 'done' };

function renderKanban(items) {
  const cols = { open: [], doing: [], done: [] };
  for (const it of items) cols[kanbanColFor(it.status)].push(it);
  const wrap = document.createElement('div');
  wrap.className = 'kanban';
  for (const [col, label] of [['open','A fazer'],['doing','Em andamento'],['done','Feito']]) {
    const k = document.createElement('div');
    k.className = 'kcol';
    k.dataset.col = col;
    k.innerHTML = `<h4>${label} (${cols[col].length})</h4>`;
    for (const it of cols[col]) {
      const c = document.createElement('div');
      c.className = 'kitem';
      c.draggable = true;
      c.dataset.id = it.sk;
      c.dataset.cat = 'task';
      c.innerHTML = `<div class="ttl">${esc(it.title)}</div>${it.due_date?`<div class="sub">📅 ${esc(it.due_date)}</div>`:''}${it.notes?`<div class="sub">${esc(it.notes).slice(0,80)}</div>`:''}`;
      c.addEventListener('click', (e) => { if (!c.classList.contains('dragging')) openItemSheet('task', it); });
      k.appendChild(c);
    }
    wrap.appendChild(k);
  }
  wrap.addEventListener('dragstart', e => { if (e.target.classList.contains('kitem')) e.target.classList.add('dragging'); });
  wrap.addEventListener('dragend', e => { if (e.target.classList.contains('kitem')) e.target.classList.remove('dragging'); });
  wrap.addEventListener('dragover', e => { e.preventDefault(); });
  wrap.addEventListener('drop', async (e) => {
    e.preventDefault();
    const dragging = wrap.querySelector('.dragging');
    const col = e.target.closest('.kcol');
    if (!dragging || !col) return;
    col.appendChild(dragging);
    const sk = dragging.dataset.id;
    const newStatus = COL_TO_STATUS[col.dataset.col] || 'open';
    // Persist (phase E stretched into phase B since kanban is front and center)
    try {
      await fetch(`/api/item/task/${encodeURIComponent(sk)}`, {
        method: 'PATCH',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({fields: {status: newStatus}}),
      });
      showToast(`✓ movido pra "${newStatus}"`, 2000);
    } catch (err) { showToast('erro ao mover: ' + err.message, 4000); }
  });
  return wrap;
}

function renderCalendar(items) {
  const cur = state.calendar.cursor;
  const y = cur.getFullYear(), m = cur.getMonth();
  const first = new Date(y, m, 1);
  const last = new Date(y, m+1, 0);
  const offset = first.getDay();
  const daysInMonth = last.getDate();
  const wrap = document.createElement('div');
  wrap.className = 'cal';
  const hdr = document.createElement('div');
  hdr.className = 'cal-hdr';
  hdr.innerHTML = `
    <div class="cal-nav">
      <button title="mês anterior" id="calPrev">‹</button>
      <button title="mês atual" id="calToday">hoje</button>
      <button title="próximo mês" id="calNext">›</button>
    </div>
    <h3 style="margin:0 0 0 6px;flex:1">${first.toLocaleString('pt-BR',{month:'long',year:'numeric'})}</h3>
    <select id="calRange" class="btn" style="padding:4px 10px">
      <option value="month">mês inteiro</option>
      <option value="7d">próximos 7 dias</option>
      <option value="30d">próximos 30 dias</option>
    </select>
    <label style="display:flex;align-items:center;gap:4px;color:var(--sub);font-size:12px;cursor:pointer">
      <input type="checkbox" id="calChartToggle" ${state.calendar.showChart?'checked':''}/> 📊 chart
    </label>`;
  wrap.appendChild(hdr);
  hdr.querySelector('#calPrev').onclick = () => { state.calendar.cursor = new Date(y, m-1, 1); renderMain(); };
  hdr.querySelector('#calNext').onclick = () => { state.calendar.cursor = new Date(y, m+1, 1); renderMain(); };
  hdr.querySelector('#calToday').onclick = () => { state.calendar.cursor = new Date(); renderMain(); };
  const rangeSel = hdr.querySelector('#calRange');
  rangeSel.value = state.calendar.range;
  rangeSel.onchange = () => { state.calendar.range = rangeSel.value; renderMain(); };
  hdr.querySelector('#calChartToggle').onchange = (e) => { state.calendar.showChart = e.target.checked; renderMain(); };

  // Range filter
  const today = new Date(); today.setHours(0,0,0,0);
  let filtered = items;
  if (state.calendar.range === '7d' || state.calendar.range === '30d') {
    const days = state.calendar.range === '7d' ? 7 : 30;
    const end = new Date(today); end.setDate(end.getDate() + days);
    filtered = items.filter(it => {
      if (!it.due_date) return false;
      const d = new Date(it.due_date + 'T12:00:00');
      return d >= today && d <= end;
    });
  }

  const grid = document.createElement('div');
  grid.className = 'cal-grid';
  for (const d of ['D','S','T','Q','Q','S','S']) {
    const h = document.createElement('div'); h.className='cal-dow'; h.textContent=d; grid.appendChild(h);
  }
  const isCurrentMonth = y === today.getFullYear() && m === today.getMonth();
  const todayDay = isCurrentMonth ? today.getDate() : -1;

  const byDate = {};
  for (const it of filtered) {
    if (!it.due_date) continue;
    const d = new Date(it.due_date + 'T12:00:00');
    if (d.getFullYear() === y && d.getMonth() === m) {
      (byDate[d.getDate()] = byDate[d.getDate()] || []).push(it);
    }
  }
  for (let i = 0; i < offset; i++) {
    const d = document.createElement('div'); d.className='cal-day other'; grid.appendChild(d);
  }
  for (let day = 1; day <= daysInMonth; day++) {
    const d = document.createElement('div');
    d.className = 'cal-day' + (day === todayDay ? ' today' : '');
    d.innerHTML = `<div class="d">${day}</div>`;
    if (byDate[day]) {
      for (const it of byDate[day].slice(0,2)) {
        const item = document.createElement('span');
        item.className = 'item';
        item.textContent = `R$${Number(it.amount||0).toFixed(0)} ${it.description||''}`;
        item.title = `${it.description || ''} — R$ ${Number(it.amount||0).toFixed(2)}${it.payee?' — '+it.payee:''}`;
        item.addEventListener('click', (e) => { e.stopPropagation(); openItemSheet('payment', it); });
        d.appendChild(item);
      }
      if (byDate[day].length > 2) {
        const more = document.createElement('span'); more.className='item'; more.textContent=`+${byDate[day].length-2}`;
        d.appendChild(more);
      }
    }
    grid.appendChild(d);
  }
  wrap.appendChild(grid);
  return wrap;
}

function renderPaymentsChart(items) {
  if (!state.calendar.showChart) return document.createComment('chart hidden');
  const wrap = document.createElement('div');
  wrap.className = 'chart-container chart-compact';
  wrap.innerHTML = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px"><h3 style="margin:0;font-size:13px;color:var(--sub)">📊 por beneficiário</h3><span style="color:var(--sub);font-size:11px">(generative UI — mesmo mecanismo que os agentes usam via render_chart)</span></div>`;
  const canvas = document.createElement('canvas');
  canvas.id = 'pay-chart-' + uid();
  canvas.height = 140;
  wrap.appendChild(canvas);
  setTimeout(() => {
    const totals = {};
    for (const it of items) {
      const payee = it.payee || it.description || 'sem nome';
      totals[payee] = (totals[payee] || 0) + Number(it.amount || 0);
    }
    const labels = Object.keys(totals);
    const values = labels.map(l => totals[l]);
    renderChartOn(canvas, { chart_type: 'bar', title: '', labels, values, unit: 'R$' });
  }, 30);
  return wrap;
}

function renderList(cat, items) {
  const wrap = document.createElement('div');
  wrap.style.background = 'var(--panel)';
  wrap.style.border = '1px solid var(--stroke)';
  wrap.style.borderRadius = '10px';
  if (items.length === 0) {
    wrap.innerHTML = `<div class="empty" style="padding:40px;color:var(--sub);text-align:center">nada aqui ainda</div>`;
    return wrap;
  }
  for (const it of items) {
    const row = document.createElement('div');
    row.style.padding = '14px 16px';
    row.style.borderBottom = '1px solid var(--stroke)';
    row.style.cursor = 'pointer';
    row.innerHTML = renderCardPreview(cat, it);
    row.addEventListener('click', () => openItemSheet(cat, it));
    wrap.appendChild(row);
  }
  return wrap;
}

// ============================================================================
// Side-sheet for item detail / edit / delete
// ============================================================================

const SHEET_FIELDS = {
  task:     [['title','texto'],['notes','textarea'],['due_date','date'],['status','select:open|doing|done']],
  payment:  [['description','texto'],['amount','number'],['due_date','date'],['payee','texto'],['status','texto']],
  reminder: [['title','texto'],['at','datetime-local'],['notes','textarea']],
  contact:  [['name','texto'],['phone','texto'],['email','texto'],['notes','textarea']],
  note:     [['title','texto'],['body','textarea']],
  email:    [['to','texto'],['subject','texto'],['body','textarea']],
};

function openItemSheet(cat, item) {
  // Mount template
  const existing = document.querySelector('.sheet');
  if (existing) existing.remove();
  const sheet = tmpl('tpl-sheet');
  document.body.appendChild(sheet);
  requestAnimationFrame(() => sheet.classList.add('open'));

  const meta = CAT_META[cat] || { label: cat, emoji: '📄' };
  sheet.querySelector('#sheetTitle').innerHTML = `${meta.emoji} ${meta.label}`;
  const body = sheet.querySelector('#sheetBody');
  const fields = SHEET_FIELDS[cat] || [];
  const inputs = {};
  for (const [name, kind] of fields) {
    const label = document.createElement('label');
    label.textContent = name.replace('_', ' ');
    body.appendChild(label);
    let inp;
    if (kind === 'textarea') inp = document.createElement('textarea');
    else if (kind.startsWith('select:')) {
      inp = document.createElement('select');
      const opts = kind.split(':')[1].split('|');
      for (const o of opts) { const opt = document.createElement('option'); opt.value = o; opt.textContent = o; inp.appendChild(opt); }
    } else {
      inp = document.createElement('input');
      inp.type = kind === 'number' ? 'number' : (kind === 'date' ? 'date' : (kind === 'datetime-local' ? 'datetime-local' : 'text'));
    }
    inp.value = item[name] != null ? String(item[name]) : '';
    inputs[name] = inp;
    body.appendChild(inp);
  }
  const metaBox = document.createElement('div');
  metaBox.className = 'meta';
  metaBox.innerHTML = `<div>id: <code style="font-size:11px">${esc(item.sk||'')}</code></div>
    <div>criado: ${item.created_at ? new Date(item.created_at*1000).toLocaleString('pt-BR') : '—'}</div>
    ${item.updated_at ? `<div>atualizado: ${new Date(item.updated_at*1000).toLocaleString('pt-BR')}</div>` : ''}`;
  body.appendChild(metaBox);

  const close = () => {
    sheet.classList.remove('open');
    setTimeout(() => sheet.remove(), 180);
  };
  sheet.querySelector('#sheetClose').onclick = close;
  sheet.querySelector('#sheetCancel').onclick = close;
  sheet.querySelector('#sheetSave').onclick = async () => {
    const payload = {};
    for (const [name] of fields) {
      const v = inputs[name].value;
      if (v !== '' && String(item[name] ?? '') !== v) {
        payload[name] = (inputs[name].type === 'number') ? Number(v) : v;
      }
    }
    if (!Object.keys(payload).length) { close(); return; }
    try {
      const r = await fetch(`/api/item/${cat}/${encodeURIComponent(item.sk)}`, {
        method: 'PATCH',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ fields: payload }),
      });
      if (!r.ok) throw new Error(await r.text());
      showToast('✓ salvo');
      close();
      refreshCards();
    } catch (e) { showToast('erro: ' + e.message, 5000); }
  };
  sheet.querySelector('#sheetDelete').onclick = async () => {
    try {
      const r = await fetch(`/api/item/${cat}/${encodeURIComponent(item.sk)}`, { method: 'DELETE' });
      if (!r.ok) throw new Error(await r.text());
      close();
      refreshCards();
      showToastWithUndo(cat, item.sk);
    } catch (e) { showToast('erro: ' + e.message, 5000); }
  };
}

// Toast helpers
let _toastTimer = null;
function showToast(html, ms = 2500) {
  const el = document.getElementById('toast');
  el.innerHTML = html;
  el.classList.remove('hidden');
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.add('hidden'), ms);
}
function showToastWithUndo(cat, sk) {
  const el = document.getElementById('toast');
  el.innerHTML = `🗑 ${cat} apagado <button id="undoBtn">desfazer</button>`;
  el.classList.remove('hidden');
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.add('hidden'), 8000);
  document.getElementById('undoBtn').onclick = async () => {
    try {
      const r = await fetch(`/api/item/${cat}/${encodeURIComponent(sk)}/undo`, { method: 'POST' });
      if (!r.ok) throw new Error(await r.text());
      showToast('↶ restaurado');
      refreshCards();
    } catch (e) { showToast('erro: ' + e.message, 5000); }
  };
}

// ============================================================================
// Chart render helper — used by both static cards and AG-UI generative UI
// ============================================================================

function renderChartOn(canvas, spec) {
  // Guard: Chart.js might not be loaded yet (we lazy-load)
  if (!window.Chart) {
    loadChartJs().then(() => renderChartOn(canvas, spec));
    return;
  }
  // If a prior Chart instance is still bound to this canvas, dispose it first
  try {
    const prior = window.Chart.getChart ? window.Chart.getChart(canvas) : null;
    if (prior) prior.destroy();
  } catch {}
  // Persist spec on the canvas so future re-renders (navigation) can recover
  try { canvas.dataset.chartSpec = JSON.stringify(spec); } catch {}
  const ctx = canvas.getContext('2d');
  const type = spec.chart_type || 'bar';
  const colors = ['#00a884','#4aa8ff','#ffb84a','#c084fc','#4ade80','#fb7185','#94a3b8'];
  new window.Chart(ctx, {
    type,
    data: {
      labels: spec.labels || [],
      datasets: [{
        label: spec.title || '',
        data: spec.values || [],
        backgroundColor: (spec.labels||[]).map((_,i)=> colors[i%colors.length]),
        borderColor: '#00a884',
        borderWidth: 1,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: type==='pie' || type==='doughnut', labels: {color:'#e9edef'} }, title: { display: !!spec.title, text: spec.title, color:'#e9edef' } },
      scales: type==='pie'||type==='doughnut' ? {} : {
        x:{ ticks:{color:'#8696a0'}, grid:{color:'#2a3942'} },
        y:{ ticks:{color:'#8696a0',callback:v=> (spec.unit||'') + v}, grid:{color:'#2a3942'} },
      },
    },
  });
}

let _chartLoading = null;
function loadChartJs() {
  if (_chartLoading) return _chartLoading;
  _chartLoading = new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js';
    s.onload = res; s.onerror = rej;
    document.head.appendChild(s);
  });
  return _chartLoading;
}

// ============================================================================
// Chat view — AG-UI SSE consumer
// ============================================================================

function getThread(agent) {
  if (!state.threads[agent]) {
    state.threads[agent] = { id: 'thread-' + agent + '-' + Math.random().toString(36).slice(2,8), messages: [], loaded: false };
  }
  return state.threads[agent];
}

async function ensureThreadLoaded(agent) {
  const t = getThread(agent);
  if (t.loaded) return t;
  try {
    const r = await fetch('/api/threads/' + encodeURIComponent(agent));
    if (r.ok) {
      const j = await r.json();
      t.messages = (j.messages || []).map(m => ({ role: m.role, html: m.html || esc(m.text || '') }));
    }
  } catch (e) {}
  t.loaded = true;
  return t;
}

async function persistMessage(agent, role, html, text) {
  try {
    await fetch('/api/threads/' + encodeURIComponent(agent), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ role, html, text: text || '' }),
    });
  } catch (e) {}
}

function endpointFor(agent) {
  return agent === 'concierge' ? '/agent' : `/agent/${encodeURIComponent(agent)}`;
}

function agentMeta(agent) {
  if (agent === 'concierge') return { display_name: 'Concierge', emoji: '🤖', description: 'Orquestrador' };
  return state.agents[agent] || { display_name: agent, emoji: '🤖' };
}

function renderChat(agent) {
  const el = tmpl('tpl-chat');
  const meta = agentMeta(agent);
  el.querySelector('#chatName').textContent = meta.display_name;
  el.querySelector('#chatAvatar').textContent = meta.emoji;
  el.querySelector('#chatSub').textContent = meta.description || 'online';

  // Lazy-load persisted history on first render of this agent
  ensureThreadLoaded(agent).then(() => {
    const msgsEl = el.querySelector('#messages');
    if (!msgsEl || msgsEl.dataset.hydrated) return;
    msgsEl.dataset.hydrated = '1';
    const thread = getThread(agent);
    // Clear prior and re-render from persisted messages if they exist
    if (thread.messages.length) {
      msgsEl.innerHTML = '';
      for (const m of thread.messages) replayMessage(msgsEl, m);
    }
  });

  // Agent quick-switch strip
  const strip = el.querySelector('#agentStrip');
  strip.innerHTML = '<span style="margin-right:4px">trocar de agente:</span>';
  for (const [name, m] of [['concierge', agentMeta('concierge')], ...Object.entries(chatAgents())]) {
    if (name === agent) continue;
    const chip = document.createElement('span');
    chip.className = 'agent-chip';
    chip.innerHTML = `${m.emoji || '🤖'} ${esc(m.display_name || name)}`;
    chip.onclick = () => navigate({ kind: 'chat', agent: name });
    strip.appendChild(chip);
  }

  // Replay thread history
  const msgs = el.querySelector('#messages');
  const thread = getThread(agent);
  for (const m of thread.messages) replayMessage(msgs, m);

  if (thread.messages.length === 0) {
    const s = document.createElement('div');
    s.className = 'msg system';
    s.textContent = agent === 'concierge'
      ? 'Bem-vindo à Caixa Postal. Manda áudio, foto, texto — eu organizo e ajo. Posso delegar pra especialistas.'
      : `Você está no chat direto com ${meta.display_name}. Nada passa pelo concierge.`;
    msgs.appendChild(s);
  }

  // Bridge context: if user clicked "refinar" on a handoff, seed a system bubble
  if (state.bridgeContext && state.bridgeContext.toAgent === agent) {
    const ctx = state.bridgeContext;
    const b = document.createElement('div');
    b.className = 'msg system';
    b.innerHTML = `🔗 contexto trazido do chat <b>${esc(ctx.fromAgent)}</b>: <em>"${esc(ctx.task)}"</em>`;
    msgs.appendChild(b);
    // Also inject as a pseudo-user message so the specialist sees the context on first send
    thread.pendingPrefix = ctx.task;
    state.bridgeContext = null;
  }

  // Wire compose
  const textIn = el.querySelector('#textIn');
  const fileIn = el.querySelector('#fileIn');
  const recBtn = el.querySelector('#recBtn');
  const sendBtn = el.querySelector('#sendBtn');
  const attachBtn = el.querySelector('#attachBtn');
  const briefBtn = el.querySelector('#briefBtn');
  const clearBtn = el.querySelector('#clearThreadBtn');

  attachBtn.onclick = () => fileIn.click();
  fileIn.onchange = () => handleFilePick(fileIn, msgs);
  recBtn.onclick = () => toggleRecord(recBtn, agent, msgs, el.querySelector('#chatSub'));
  sendBtn.onclick = () => doSend(agent, textIn, msgs, el.querySelector('#chatSub'));
  textIn.addEventListener('keydown', e => { if (e.key === 'Enter') sendBtn.click(); });
  briefBtn.onclick = () => loadBriefing(msgs, el.querySelector('#chatSub'));
  clearBtn.onclick = async () => {
    if (!confirm('Esquecer o histórico dessa conversa? (apaga do DynamoDB também)')) return;
    try { await fetch('/api/threads/' + encodeURIComponent(agent), { method: 'DELETE' }); } catch (e) {}
    state.threads[agent] = { id: 'thread-' + agent + '-' + Math.random().toString(36).slice(2,8), messages: [], loaded: true };
    renderMain();
  };

  // Auto-briefing on first concierge open (emulates 9h scheduled)
  if (agent === 'concierge' && thread.messages.length === 0 && !thread.briefed) {
    thread.briefed = true;
    setTimeout(() => loadBriefing(msgs, el.querySelector('#chatSub')), 600);
  }

  return el;
}

function replayMessage(msgs, m) {
  const b = document.createElement('div');
  b.className = 'msg ' + (m.role === 'user' ? 'me' : m.role === 'assistant' ? 'other' : 'system');
  b.innerHTML = m.html;
  msgs.appendChild(b);
}

function bubble(msgs, {role, html, meta}) {
  const el = document.createElement('div');
  el.className = 'msg ' + (role === 'user' ? 'me' : role === 'assistant' ? 'other' : 'system');
  el.innerHTML = html + (meta ? `<span class="time">${meta}</span>` : '');
  msgs.appendChild(el);
  msgs.scrollTop = msgs.scrollHeight;
  return el;
}

async function loadBriefing(msgs, statusEl) {
  statusEl.textContent = 'buscando briefing…';
  try {
    const r = await fetch('/api/briefing');
    const j = await r.json();
    const text = (j.text || '').trim();
    if (!text) return;
    const html = `<div class="handoff"><span>🌅</span><span>briefing por</span><span>📅 agenda</span><span class="arrow">▸</span><span>enviado ao chat</span></div>${esc(text).replace(/\n/g,'<br>')}<span class="time">${now()}</span>`;
    bubble(msgs, { role: 'assistant', html, meta: '' });
    getThread('concierge').messages.push({ role:'assistant', html });
  } catch(e) {
    bubble(msgs, { role: 'system', html: 'Erro: ' + esc(e.message) });
  } finally {
    statusEl.textContent = 'online';
  }
}

async function handleFilePick(fileIn, msgs) {
  const f = fileIn.files[0];
  if (!f) return;
  const dataUrl = URL.createObjectURL(f);
  const b64 = await fileToB64(f);
  state.pendingImage = { dataUrl, base64: b64, mime: f.type || 'image/jpeg' };
  bubble(msgs, { role: 'user', html: `<img src="${dataUrl}"/><div style="font-size:12px;color:#8696a0;margin-top:4px">📎 imagem anexada — agora descreva</div>`, meta: now() });
  fileIn.value = '';
}

async function toggleRecord(btn, agent, msgs, statusEl) {
  if (state.recording) {
    state.recording.stop();
    btn.classList.remove('rec');
    btn.textContent = '🎤';
    statusEl.textContent = 'processando áudio…';
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio:true});
    const mr = new MediaRecorder(stream, { mimeType: 'audio/webm' });
    const chunks = [];
    mr.ondataavailable = e => e.data.size && chunks.push(e.data);
    mr.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      state.recording = null;
      const blob = new Blob(chunks, { type: 'audio/webm' });
      const url = URL.createObjectURL(blob);
      bubble(msgs, { role: 'user', html: `<audio controls src="${url}"></audio><div style="font-size:12px;color:#8696a0;margin-top:4px">🎤 transcrevendo…</div>`, meta: now() });
      const fd = new FormData();
      fd.append('audio', blob, 'rec.webm');
      try {
        const r = await fetch('/api/transcribe', { method: 'POST', body: fd });
        const j = await r.json();
        if (!j.text) throw new Error('transcribe vazio');
        bubble(msgs, { role: 'system', html: `Transcrição: "${esc(j.text)}"` });
        await sendToAgent(agent, j.text, null, msgs, statusEl);
      } catch (e) {
        bubble(msgs, { role: 'system', html: 'Erro ao transcrever: ' + esc(e.message) });
      } finally {
        statusEl.textContent = 'online';
      }
    };
    mr.start();
    state.recording = mr;
    btn.classList.add('rec');
    btn.textContent = '⏹';
    statusEl.textContent = '🔴 gravando — clique pra parar';
  } catch (e) {
    alert('Microfone indisponível: ' + e.message);
  }
}

function fileToB64(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result.split(',')[1]);
    r.onerror = rej;
    r.readAsDataURL(file);
  });
}

async function doSend(agent, textIn, msgs, statusEl) {
  const txt = textIn.value.trim();
  const img = state.pendingImage;
  if (!txt && !img) return;
  textIn.value = '';
  if (txt && !img) {
    const html = esc(txt);
    bubble(msgs, { role: 'user', html, meta: now() });
    getThread(agent).messages.push({ role: 'user', html });
    persistMessage(agent, 'user', html, txt);
  }
  state.pendingImage = null;
  await sendToAgent(agent, txt || 'Analisa essa imagem.', img, msgs, statusEl);
}

async function sendToAgent(agent, text, image, msgs, statusEl) {
  const thread = getThread(agent);
  // Apply bridge prefix if present (context brought in from another chat)
  let finalText = text;
  if (thread.pendingPrefix) {
    finalText = `[contexto trazido de outro chat: "${thread.pendingPrefix}"]\n\n${text}`;
    thread.pendingPrefix = null;
  }

  const content = [];
  if (finalText) content.push({ type: 'text', text: finalText });
  if (image) content.push({ type: 'image', source: { type: 'data', value: image.base64, mime_type: image.mime || 'image/jpeg' } });
  const userMsg = {
    id: uid(),
    role: 'user',
    content: (content.length === 1 && content[0].type === 'text') ? finalText : content,
  };

  // Push to AG-UI messages array for this thread
  const agMessages = (thread.agMessages = thread.agMessages || []);
  agMessages.push(userMsg);

  const typingEl = bubble(msgs, { role: 'assistant', html: '<span class="typing">digitando…</span>' });
  let asstBubble = null;
  let asstText = '';
  const toolCards = new Map();
  statusEl.textContent = 'processando…';

  try {
    const resp = await fetch(endpointFor(agent), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
      body: JSON.stringify({
        thread_id: thread.id,
        run_id: 'run-' + Math.random().toString(36).slice(2,8),
        messages: agMessages,
        tools: [], context: [], state: {}, forwarded_props: {},
      }),
    });
    if (!resp.ok) throw new Error(`${resp.status}: ${(await resp.text()).slice(0,200)}`);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';

    const getAsst = () => {
      if (!asstBubble) {
        typingEl.remove();
        asstBubble = bubble(msgs, { role: 'assistant', html: '', meta: '' });
        // Structured layout: <div.text> + appended tool-cards/handoffs + <span.time>
        const textEl = document.createElement('div');
        textEl.className = 'text-part';
        asstBubble.appendChild(textEl);
        const timeEl = document.createElement('span');
        timeEl.className = 'time';
        timeEl.textContent = now();
        asstBubble.appendChild(timeEl);
        asstBubble.dataset.text = '';
        asstBubble._textEl = textEl;
        asstBubble._timeEl = timeEl;
      }
      return asstBubble;
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) !== -1) {
        const frame = buf.slice(0, idx); buf = buf.slice(idx+2);
        const lines = frame.split('\n');
        let data = '';
        for (const l of lines) if (l.startsWith('data:')) data += l.slice(5).trim();
        if (!data) continue;
        let evt;
        try { evt = JSON.parse(data); } catch { continue; }
        handleEvt(evt, { getAsst, toolCards, agent });
      }
    }
    if (asstText) {
      agMessages.push({ id: uid(), role: 'assistant', content: asstText });
      const html = asstBubble?.innerHTML || esc(asstText);
      thread.messages.push({ role: 'assistant', html });
      persistMessage(agent, 'assistant', html, asstText);
    }
    refreshCards(); // pick up anything created
  } catch (e) {
    typingEl.remove();
    bubble(msgs, { role: 'assistant', html: '⚠️ erro: ' + esc(e.message) });
  } finally {
    statusEl.textContent = 'online';
  }

  function refresh(el) {
    // Update only the text part; tool-cards/handoffs stay as live DOM nodes (so Chart.js, iframes, etc. survive)
    if (el._textEl) {
      el._textEl.innerHTML = esc(el.dataset.text).replace(/\n/g,'<br>');
    } else {
      // fallback
      el.textContent = el.dataset.text;
    }
    if (el._timeEl) el._timeEl.textContent = now();
    msgs.scrollTop = msgs.scrollHeight;
  }

  function handleEvt(evt, ctx) {
    const type = evt.type || evt.event_type || evt.eventType;
    switch (type) {
      case 'TEXT_MESSAGE_START': ctx.getAsst(); break;
      case 'TEXT_MESSAGE_CONTENT': {
        const el = ctx.getAsst();
        const delta = evt.delta || '';
        asstText += delta;
        el.dataset.text += delta;
        refresh(el);
        break;
      }
      case 'TOOL_CALL_START': {
        const el = ctx.getAsst();
        const id = evt.tool_call_id || evt.toolCallId;
        const name = evt.tool_call_name || evt.toolCallName || '?';
        let card;
        if (name === 'call_specialist') {
          card = document.createElement('div');
          card.className = 'handoff';
          card.dataset.toolName = name;
          card.innerHTML = `<span>🤝</span><span>${esc(ctx.agent)}</span><span class="arrow">▸</span><span class="ho-target">especialista…</span>`;
        } else if (name === 'generate_pitch_deck') {
          card = document.createElement('div');
          card.className = 'tool-card pitch-building';
          card.dataset.toolName = name;
          card.innerHTML = `<div class="t-name">🎯 gerando pitch deck</div><div class="t-args" style="opacity:.6">estruturando e desenhando slides em paralelo…</div>`;
        } else if (name === 'render_chart') {
          card = document.createElement('div');
          card.className = 'tool-card chart-card';
          card.dataset.toolName = name;
          card.innerHTML = `<div class="t-name">📊 render_chart</div><div class="t-args" style="opacity:.6">desenhando gráfico…</div>`;
        } else {
          card = document.createElement('div');
          card.className = 'tool-card';
          card.dataset.toolName = name;
          card.innerHTML = `<div class="t-name">🛠 ${esc(name)}</div><div class="t-args" style="opacity:.6">…</div>`;
        }
        card.dataset.id = id;
        // Insert tool/handoff cards BEFORE the time span so they appear with the text
        if (el._timeEl) el.insertBefore(card, el._timeEl); else el.appendChild(card);
        ctx.toolCards.set(id, card);
        break;
      }
      case 'TOOL_CALL_ARGS': {
        const id = evt.tool_call_id || evt.toolCallId;
        const card = ctx.toolCards.get(id);
        if (!card) break;
        card.dataset.raw = (card.dataset.raw || '') + (evt.delta || '');
        try {
          const parsed = JSON.parse(card.dataset.raw);
          if (card.classList.contains('handoff') && parsed.agent_name) {
            card.querySelector('.ho-target').textContent = '📦 ' + parsed.agent_name;
          } else if (card.classList.contains('chart-card')) {
            card.querySelector('.t-args').textContent = `${parsed.chart_type || ''} • ${(parsed.labels||[]).length} pontos`;
          } else {
            const argsEl = card.querySelector('.t-args');
            if (argsEl) argsEl.textContent = Object.entries(parsed).map(([k,v])=>`${k}: ${typeof v==='string'?v.slice(0,60):JSON.stringify(v).slice(0,60)}`).join(' • ');
          }
        } catch {}
        break;
      }
      case 'TOOL_CALL_END': { /* args finalized */ break; }
      case 'MESSAGES_SNAPSHOT': {
        // Some AG-UI adapters (incl. aws-strands) deliver tool results as ToolMessages inside a messages snapshot,
        // instead of TOOL_CALL_RESULT. Scan for them and reroute to enhanceCard.
        const msgs = evt.messages || [];
        for (const m of msgs) {
          if (m && m.role === 'tool' && m.tool_call_id) {
            const card = ctx.toolCards.get(m.tool_call_id);
            if (!card) continue;
            let res = null;
            if (m.content) { try { res = JSON.parse(m.content); } catch { res = m.content; } }
            enhanceCard(card, res, ctx.agent);
          }
        }
        break;
      }
      case 'STATE_SNAPSHOT': {
        // If the agent emitted dashboard_cards in shared state, ingest them into our snapshot store.
        const dc = evt.snapshot && evt.snapshot.dashboard_cards;
        if (dc && typeof dc === 'object') {
          for (const [ag, keys] of Object.entries(dc)) {
            for (const [key, spec] of Object.entries(keys)) {
              state.snapshots[ag] = state.snapshots[ag] || {};
              state.snapshots[ag][key] = spec;
              layoutAdd(ag, key);
            }
          }
          saveSnapshots();
        }
        break;
      }
      case 'TOOL_CALL_RESULT': {
        const id = evt.tool_call_id || evt.toolCallId;
        const card = ctx.toolCards.get(id);
        if (!card) break;
        let result = null;
        if (evt.content) { try { result = JSON.parse(evt.content); } catch { result = evt.content; } }
        enhanceCard(card, result, ctx.agent);
        break;
      }
      default: break;
    }
  }
}

function enhanceCard(card, result, fromAgent) {
  const name = card.dataset.toolName;
  card.style.borderColor = '#4aff9f';

  if (name === 'call_specialist' && result) {
    // Expose a "refinar com X" button; use data-* so the handler survives text streaming re-renders (event delegation in main).
    const target = result.agent || '';
    let raw = {};
    try { raw = JSON.parse(card.dataset.raw || '{}'); } catch {}
    const taskPreview = (raw.task || '').slice(0, 400);
    const existing = card.querySelector('.refine');
    if (!existing) {
      const b = document.createElement('button');
      b.className = 'refine';
      b.dataset.refineTo = target;
      b.dataset.refineFrom = fromAgent;
      b.dataset.refineTask = taskPreview;
      b.textContent = `🔗 refinar com ${target}`;
      card.appendChild(b);
    }
    return;
  }

  if (name === 'generate_pitch_deck' && result && result.ok && result.pitch_id) {
    card.classList.remove('pitch-building');
    card.classList.add('pitch-ready');
    const url = result.preview_url || ('/api/pitch/' + result.pitch_id);
    card.innerHTML = `
      <div class="t-name">🎯 ${esc(result.title || 'pitch deck')} — ${result.slide_count || 6} slides</div>
      <div style="margin:6px 0 8px;opacity:.85">${esc(result.tagline || '')}</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <a href="${url}" target="_blank" style="background:var(--accent);color:white;padding:6px 12px;border-radius:6px;text-decoration:none;font-weight:600;font-size:12px">▶ abrir deck</a>
        <button type="button" onclick="togglePitchPreview('${result.pitch_id}', this)" style="background:transparent;color:#b3f0e0;border:1px solid var(--accent);padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px">👁 preview</button>
      </div>
      <div class="pitch-preview" id="pp-${result.pitch_id}" style="display:none;margin-top:10px;border-radius:6px;overflow:hidden;background:#050505"></div>
    `;
    return;
  }

  if (name === 'render_chart' && result && result.ok) {
    card.innerHTML = `<div class="t-name">📊 ${esc(result.title || 'gráfico')}</div><div style="background:#050505;border-radius:6px;padding:10px;margin-top:8px"><canvas></canvas></div>`;
    const canvas = card.querySelector('canvas');
    canvas.height = 220;
    renderChartOn(canvas, result);
    return;
  }

  if (name === 'publish_card' && result && result.ok && result.agent && result.key && result.card) {
    
    const ag = result.agent;
    const key = result.key;
    const cardData = result.card;
    state.snapshots[ag] = state.snapshots[ag] || {};
    state.snapshots[ag][key] = cardData;
    saveSnapshots();
    // Auto-add to home layout if not there yet — user explicitly asked to publish
    layoutAdd(ag, key);
    card.innerHTML = `<div class="t-name">🗂 publicado no home</div>
      <div style="margin-top:6px;font-size:13px">
        📌 <b>${esc(cardData.title)}</b> (${esc(cardData.template)}) — por <b>${esc(ag)}</b>
      </div>
      <div style="font-size:12px;color:var(--sub);margin-top:4px">Já está fixado na home — abra a home pra ver.</div>`;
    return;
  }
}

// Full HTML-entity escape for srcdoc attribute — replaceAll('"','&quot;') alone leaves
// <, >, &, ' unescaped and opens XSS paths (P0.11). Use the entity map for the 5 HTML-sensitive chars.
function escAttr(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

window.togglePitchPreview = async function(pitchId, btn) {
  const container = document.getElementById('pp-' + pitchId);
  if (!container) return;
  if (container.style.display === 'none') {
    if (!container.innerHTML) {
      container.innerHTML = '<div style="padding:20px;text-align:center;opacity:.6">carregando preview…</div>';
      try {
        const r = await fetch('/api/pitch/' + pitchId);
        const html = await r.text();
        // srcdoc body must be a single attribute value; escape all HTML entities (P0.11)
        const iframe = document.createElement('iframe');
        iframe.setAttribute('srcdoc', html);
        iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin');
        iframe.style = 'width:100%;height:340px;border:none;display:block';
        container.innerHTML = '';
        container.appendChild(iframe);
      } catch (e) {
        container.innerHTML = '<div style="padding:20px;color:#ff8a8a">erro: ' + escAttr(e.message) + '</div>';
      }
    }
    container.style.display = 'block';
    btn.textContent = '🙈 ocultar';
  } else {
    container.style.display = 'none';
    btn.textContent = '👁 preview';
  }
};

// ============================================================================
// Data refresh
// ============================================================================

async function refreshCards() {
  try {
    state.cardData = await _fetchWithRetry('/api/ops');
    renderSidebar();
    if (state.view.kind === 'home') renderMain();
  } catch (e) {
    state.cardData = state.cardData || {};
    _hydrationErrors.push({ what: 'ops', e });
  }
}

async function loadAgentsList() {
  try {
    const j = await _fetchWithRetry('/api/agents');
    state.agents = j.agents || {};
    renderSidebar();
  } catch (e) {
    state.agents = state.agents || {};
    _hydrationErrors.push({ what: 'agents', e });
  }
}

function chatAgents() {
  // agents discoverable as chat threads — excludes __core__
  return Object.fromEntries(Object.entries(state.agents).filter(([k]) => k !== '__core__'));
}

// Retry wrapper for boot-time hydration calls. If any fail, we keep safe defaults
// (empty state) and show a non-blocking "reconectando…" banner that disappears when
// the next retry succeeds (P0.10).
async function _fetchWithRetry(url, options = {}, tries = 3) {
  let lastErr;
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(url, options);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } catch (e) {
      lastErr = e;
      if (i < tries - 1) await new Promise(r => setTimeout(r, 400 * Math.pow(2, i)));
    }
  }
  throw lastErr;
}

async function loadMe() {
  try {
    state.me = await _fetchWithRetry('/api/me', { credentials: 'include' });
  } catch (e) {
    // Keep the default anon me so UI renders; flag degraded mode
    state.me = state.me || { user: { id: 'anon', email: '', name: 'Anônimo' }, auth_enabled: false, authenticated: false };
    _hydrationErrors.push({ what: 'me', e });
  }
}

const _hydrationErrors = [];
function showHydrationBanner() {
  if (!_hydrationErrors.length) return hideHydrationBanner();
  let el = document.getElementById('hydrateBanner');
  if (!el) {
    el = document.createElement('div');
    el.id = 'hydrateBanner';
    el.style = 'position:fixed;top:0;left:0;right:0;background:#2a1e1e;border-bottom:1px solid #ff8a8a;color:#ffcccc;padding:6px 14px;font-size:12px;text-align:center;z-index:200';
    document.body.appendChild(el);
  }
  el.innerHTML = `⚠ conectividade instável (${_hydrationErrors.length} falha${_hydrationErrors.length>1?'s':''}) — reconectando… <button id="retryHydrate" style="background:#ff8a8a;color:#1a1a1a;border:none;padding:2px 8px;border-radius:4px;cursor:pointer;margin-left:8px">tentar agora</button>`;
  document.getElementById('retryHydrate').onclick = () => { _hydrationErrors.length = 0; refreshAll().then(() => showHydrationBanner()); };
}
function hideHydrationBanner() {
  const el = document.getElementById('hydrateBanner');
  if (el) el.remove();
}

async function refreshAll() {
  _hydrationErrors.length = 0;
  await Promise.all([loadMe(), refreshCards(), loadAgentsList()]);
  showHydrationBanner();
}

// Intercept fetch responses to auto-redirect to /auth/login on 401
const _origFetch = window.fetch;
window.fetch = async function(url, opts) {
  const r = await _origFetch.call(this, url, opts);
  if (r.status === 401 && state.me && state.me.auth_enabled) {
    window.location.href = '/auth/login';
    return new Response('{}', { status: 401 });
  }
  return r;
};

// ============================================================================
// Boot
// ============================================================================

// Render user pill in sidebar footer when authenticated
function renderUserPill() {
  const footer = document.querySelector('.side-footer');
  if (!footer) return;
  if (!state.me || !state.me.auth_enabled) return;
  if (state.me.authenticated) {
    const u = state.me.user || {};
    const existing = document.getElementById('userPill');
    if (existing) existing.remove();
    const pill = document.createElement('div');
    pill.id = 'userPill';
    pill.style = 'display:flex;align-items:center;gap:8px;padding:8px 6px;margin-bottom:8px;background:var(--bg);border:1px solid var(--stroke);border-radius:8px;font-size:12px';
    pill.innerHTML = `
      <div style="width:28px;height:28px;border-radius:999px;background:var(--accent);display:flex;align-items:center;justify-content:center;color:white;font-weight:700">${esc((u.name || u.email || '?')[0].toUpperCase())}</div>
      <div style="flex:1;min-width:0;overflow:hidden"><div style="font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(u.name || u.email || '')}</div><div style="color:var(--sub);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(u.email || '')}</div></div>
      <a href="/auth/logout" title="sair" style="color:var(--sub);text-decoration:none;padding:4px 6px">⎋</a>`;
    footer.insertBefore(pill, footer.firstChild);
  }
}

$('addAgentBtn').onclick = (e) => {
  e.preventDefault();
  window.open('/desafio', '_blank');
};

$$('.nav-item').forEach(el => {
  el.addEventListener('click', () => {
    if (el.dataset.view === 'home') navigate({kind:'home'});
  });
});

// Event delegation for refine buttons — survives text streaming re-renders
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.refine');
  if (!btn) return;
  e.stopPropagation();
  e.preventDefault();
  const toAgent = btn.dataset.refineTo;
  const fromAgent = btn.dataset.refineFrom;
  const task = btn.dataset.refineTask || '';
  if (!toAgent) return;
  state.bridgeContext = { fromAgent, toAgent, task };
  navigate({ kind: 'chat', agent: toAgent });
});

window.addEventListener('load', async () => {
  await refreshAll();
  loadChartJs();  // prefetch
  navigate({kind:'home'});
  // Poll for card updates while on home — jitter 5-7s to avoid synchronized
  // thundering herd against /api/ops at :00, :05, :10, ... of each minute.
  const scheduleNextPoll = () => {
    const delay = 5000 + Math.floor(Math.random() * 2000);
    setTimeout(() => {
      if (state.view.kind === 'home') refreshCards();
      scheduleNextPoll();
    }, delay);
  };
  scheduleNextPoll();
});
