const $ = (id) => document.getElementById(id);
const state = { token: '', tenant: 'demo-shop', cases: [], selected: null, detail: null, busy: false,
  listGeneration: 0, detailGeneration: 0, accessToken: '', principal: null, authMode: 'demo', sessionGeneration: 0,
  view: 'inbox', operationsGeneration: 0, historyGeneration: 0, batchGeneration: 0,
  fileGeneration: { import: 0, reconcile: 0 }, importBusy: false, reconcileBusy: false };
const can = (role) => state.authMode === 'demo' || state.principal?.role === role;
const money = (pence) => new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' }).format(Number(pence || 0) / 100);
const human = (value) => String(value || '').replaceAll('_', ' ').replace(/^./, (c) => c.toUpperCase());
const done = (status) => ['resolved', 'refunded', 'partially_refunded', 'closed', 'executed', 'completed'].includes(status);
const fixtureLabels = {
  'case-eligible': ['Customer 01', 'Return an unused item'],
  'case-approval': ['Customer 02', 'Damaged delivery'],
  'case-expired': ['Customer 03', 'Return outside the window'],
  'case-injection': ['Customer 04', 'Unusual refund request'],
  'case-refunded': ['Customer 05', 'Follow-up on a refund'],
  'case-lookalike': ['Customer 06', 'Check an order'],
};
const customerLabel = (item) => item.customer_name || fixtureLabels[item.id]?.[0] || human(item.customer_id || 'Customer');
const subjectLabel = (item) => item.subject || fixtureLabels[item.id]?.[1] || 'Order support request';
const node = (tag, className = '', text = '') => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== '') element.textContent = String(text);
  return element;
};
const append = (parent, ...children) => { parent.append(...children); return parent; };
const badge = (text, color = '') => node('span', `badge ${color}`, text);

async function request(path, body) {
  const session = state.sessionGeneration;
  const options = { headers: {} };
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = { 'Content-Type': 'application/json', 'X-CSRF-Token': state.token };
    options.body = JSON.stringify(body);
  }
  if (state.accessToken) options.headers.Authorization = `Bearer ${state.accessToken}`;
  const response = await fetch(path, options);
  const data = await response.json();
  if (session !== state.sessionGeneration) throw new Error('The signed-in identity changed.');
  if (response.status === 401) showLogin();
  if (!response.ok) throw new Error(data.error?.message || 'The request could not be completed.');
  return data;
}

function notice(message = '', error = false) {
  $('notice').textContent = message;
  $('notice').className = `notice ${message ? '' : 'hidden'} ${error ? 'error' : ''}`;
}

function renderCases() {
  $('case-list').replaceChildren();
  $('stat-total').textContent = state.cases.length;
  $('nav-count').textContent = state.cases.length;
  $('stat-review').textContent = state.cases.filter((c) => !done(c.status) && c.status !== 'escalated').length;
  $('stat-complete').textContent = state.cases.filter((c) => done(c.status) || c.status === 'escalated').length;
  const visibleCases = state.cases.slice(0, 200);
  const selectedCase = state.cases.find((item) => item.id === state.selected);
  if (selectedCase && !visibleCases.some((item) => item.id === state.selected)) visibleCases.push(selectedCase);
  for (const item of visibleCases) {
    const card = node('button', `case-card ${state.selected === item.id ? 'selected' : ''}`);
    card.type = 'button';
    card.setAttribute('aria-pressed', String(state.selected === item.id));
    const top = append(node('div', 'case-top'), node('span', '', customerLabel(item)),
      node('span', 'case-id', item.id.replace('case-', '#')));
    append(card, top, node('h3', '', subjectLabel(item)),
      node('p', '', item.customer_message || 'Review the customer request and order.'),
      badge(human(item.status || 'open'), done(item.status) || item.status === 'escalated' ? 'green' : ''));
    card.addEventListener('click', () => selectCase(item.id));
    $('case-list').append(card);
  }
  if (state.cases.length > 200) $('case-list').append(node('p', 'field-hint', `Showing the first 200 of ${state.cases.length} requests, plus the selected case if needed.`));
  if (!state.cases.length) $('case-list').append(node('div', 'empty-state', 'No requests in this workspace.'));
}

async function refresh() {
  const tenant = state.tenant;
  const generation = ++state.listGeneration;
  const response = await request(`/api/cases?tenant=${encodeURIComponent(tenant)}`);
  if (state.tenant !== tenant || state.listGeneration !== generation) return;
  state.cases = response.cases;
  if (!state.cases.some((item) => item.id === state.selected)) state.selected = state.cases[0]?.id;
  renderCases();
  if (state.selected) await selectCase(state.selected, false);
}

async function selectCase(id, clearNotice = true) {
  if (state.busy) return;
  if (clearNotice) notice();
  state.selected = id;
  state.detail = null;
  const generation = ++state.detailGeneration;
  $('detail-panel').replaceChildren(node('div', 'empty-state', 'Loading request…'));
  renderCases();
  try {
    const tenant = state.tenant;
    const detail = await request(`/api/cases/${encodeURIComponent(id)}?tenant=${encodeURIComponent(tenant)}`);
    if (state.selected !== id || state.tenant !== tenant || state.detailGeneration !== generation) return;
    state.detail = detail;
    renderDetail();
  } catch (error) {
    if (state.selected === id && state.detailGeneration === generation) notice(error.message, true);
  }
}

function renderDetail() {
  const { case: item, policy = {}, audit = [] } = state.detail;
  const panel = $('detail-panel');
  panel.replaceChildren();
  const intro = append(node('div'), node('div', 'eyebrow', `${item.id} · ${item.order?.id || 'ORDER'}`),
    node('h2', '', subjectLabel(item)),
    node('p', '', customerLabel(item)));
  const header = append(node('div', 'detail-header'), intro, badge(human(item.status), done(item.status) ? 'green' : ''));
  const body = node('div', 'detail-body');
  append(body, node('h3', 'section-label', 'CUSTOMER MESSAGE'), node('div', 'message-box', item.customer_message));
  const order = item.order || {};
  const grid = node('div', 'order-grid');
  for (const [label, value] of [['Order total', money(order.total_pence)], ['Already refunded', money(order.refunded_pence)], ['Available to refund', money(order.remaining_pence)]]) {
    grid.append(append(node('div'), node('span', '', label), node('strong', '', value)));
  }
  append(body, node('h3', 'section-label', 'ORDER & POLICY'), grid);
  const days = policy.refund_window_days ?? policy.window_days;
  const threshold = policy.approval_threshold_pence ?? policy.auto_approve_limit_pence ?? policy.auto_refund_limit_pence;
  const rules = [];
  if (days !== undefined) rules.push(`Refunds within ${days} days of purchase.`);
  if (threshold !== undefined) rules.push(`Refunds above ${money(threshold)} require approval.`);
  rules.push(`Case revision ${item.version}. Order revision ${order.version ?? 0}. Policy revision ${policy.version ?? 'current'}.`);
  body.append(node('p', 'policy-note', rules.join(' ')));
  if (can('operator')) body.append(actionForm(item));
  else body.append(node('p', 'policy-note', state.principal?.role === 'reviewer'
    ? 'You can review and approve proposals. An operator creates and executes actions.'
    : 'You have read-only access to cases and their review history.'));
  const proposals = item.proposals || [];
  for (const proposal of [...proposals].reverse()) body.append(proposalCard(proposal, item));
  body.append(timeline(audit));
  append(panel, header, body);
}

function actionForm(item) {
  const context = { tenant: item.tenant, case_id: item.id };
  const section = node('section', 'action-section');
  section.append(node('h3', 'section-label', 'PROPOSE A RESOLUTION'));
  const form = node('form');
  const fields = node('div', 'form-row');
  const left = node('div');
  const actionLabel = node('label', '', 'Action'); actionLabel.htmlFor = 'action';
  const action = node('select'); action.id = 'action';
  for (const [value, title] of [['refund', 'Issue a refund'], ['escalate', 'Escalate to a specialist']]) {
    const option = node('option', '', title); option.value = value; action.append(option);
  }
  const amountLabel = node('label', '', 'Refund amount (£)'); amountLabel.htmlFor = 'amount';
  const amount = node('input'); amount.id = 'amount'; amount.type = 'number'; amount.min = '0.01'; amount.step = '0.01';
  amount.value = ((item.order?.remaining_pence || 0) / 100).toFixed(2);
  amount.required = true;
  append(left, actionLabel, action, node('div', 'field-gap'), amountLabel, amount);
  action.addEventListener('change', () => { amount.disabled = action.value === 'escalate'; amount.required = !amount.disabled; });
  const right = node('div');
  const reasonLabel = node('label', '', 'Reason for this action'); reasonLabel.htmlFor = 'reason';
  const reason = node('textarea'); reason.id = 'reason'; reason.required = true; reason.maxLength = 300;
  reason.placeholder = 'Record why this is the right resolution.';
  append(right, reasonLabel, reason); append(fields, left, right);
  const suggest = node('button', 'secondary', 'Draft with local AI'); suggest.type = 'button';
  suggest.addEventListener('click', async () => {
    const tenant = context.tenant; const caseId = context.case_id;
    const session = state.sessionGeneration;
    suggest.disabled = true; suggest.textContent = 'Drafting suggestion…'; notice();
    try {
      const draft = await request('/api/suggest', { tenant, case_id: caseId });
      if (state.selected !== caseId || state.tenant !== tenant || session !== state.sessionGeneration) return;
      action.value = draft.action;
      amount.value = (draft.amount_pence / 100).toFixed(2);
      amount.disabled = draft.action === 'escalate'; amount.required = !amount.disabled;
      reason.value = draft.reason;
      notice('Suggestion drafted. Review the amount and reason, then check the proposal. No action has been taken.');
    } catch (error) { if (state.selected === caseId && state.tenant === tenant && session === state.sessionGeneration) notice(error.message, true); }
    finally { suggest.disabled = false; suggest.textContent = 'Draft with local AI'; }
  });
  const suggestionRow = append(node('div', 'suggestion-row'), suggest, node('span', '', 'Optional draft. You review every action.'));
  const submit = node('button', 'primary', 'Check & propose'); submit.type = 'submit';
  const footer = append(node('div', 'action-footer'), node('p', '', 'Checks the current order and policy. No funds move at this step.'), submit);
  append(form, fields, suggestionRow, footer);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const amountPence = action.value === 'refund' ? Math.round(Number(amount.value) * 100) : 0;
    await mutate('/api/propose', { action: action.value, amount_pence: amountPence, reason: reason.value,
      expected_version: item.version, idempotency_key: crypto.randomUUID() }, 'Proposal created. Review the action below.', context);
  });
  section.append(form);
  return section;
}

function proposalCard(proposal, item) {
  const context = { tenant: item.tenant, case_id: item.id };
  const invalid = ['rejected', 'invalid', 'expired', 'stale'].includes(proposal.status);
  const card = node('section', `proposal-box ${invalid ? 'invalid' : ''}`);
  const title = proposal.action === 'refund' ? `Refund ${money(proposal.amount_pence)}` : 'Escalate to a specialist';
  append(card, append(node('div', 'proposal-top'), node('h3', '', title), badge(human(proposal.status || 'proposed'), invalid ? 'red' : 'green')),
    node('p', '', proposal.reason || 'No reason recorded.'));
  const completed = ['executed', 'completed'].includes(proposal.status);
  const approved = proposal.status === 'approved';
  card.append(node('p', '', completed ? 'This action has been completed and recorded.' : invalid ? 'This proposal cannot be executed. Create a new proposal from the current case.' :
    proposal.requires_approval && !approved ? 'Policy checks passed. A reviewer must approve this amount before execution.' :
      approved ? 'Review approved. The order and policy will be checked again before execution.' : 'Policy checks passed. Ready to execute after your review.'));
  if (!invalid && !completed) {
    const controls = node('div', 'proposal-controls');
    if (proposal.requires_approval && !approved && can('reviewer')) {
      const approve = node('button', 'secondary', 'Approve as reviewer'); approve.type = 'button';
      approve.addEventListener('click', () => mutate('/api/approve', { proposal_id: proposal.id }, 'Approval recorded. You can now execute the action.', context));
      controls.append(approve);
    }
    if (can('operator')) {
      const execute = node('button', 'primary', 'Execute action'); execute.type = 'button';
      execute.disabled = Boolean(proposal.requires_approval && !approved);
      execute.addEventListener('click', () => mutate('/api/execute', { proposal_id: proposal.id,
        idempotency_key: `execute-${proposal.id}` }, 'Action completed. The order and review trail have been updated.', context));
      controls.append(execute);
    }
    card.append(controls);
  }
  return card;
}

function timeline(events) {
  const section = node('section', 'timeline');
  section.append(node('h3', 'section-label', 'ACTIVITY & REVIEW TRAIL'));
  if (!events.length) section.append(node('p', 'policy-note', 'No actions yet. The customer request is ready for review.'));
  for (const event of [...events].reverse()) {
    const entry = node('div', 'timeline-entry');
    const title = human(event.event_type || event.event || event.action || event.kind || 'Record updated');
    const details = typeof event.facts === 'object' ? event.facts : typeof event.details === 'object' ? event.details : {};
    const time = event.created_at || event.timestamp || event.at;
    const info = [];
    if (time) info.push(new Date(time).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' }));
    if (event.actor || details.actor_label) info.push(human(event.actor || details.actor_label));
    if (details.amount_pence !== undefined) info.push(money(details.amount_pence));
    if (details.reason || event.reason) info.push(details.reason || event.reason);
    append(entry, node('strong', '', title), node('p', '', info.join(' · ') || 'Recorded in the case history.'));
    section.append(entry);
  }
  return section;
}

async function mutate(path, fields, successMessage, context) {
  if (state.busy) return;
  if (!context || state.tenant !== context.tenant || state.selected !== context.case_id) {
    notice('The selected request changed. Review it before taking an action.', true);
    return;
  }
  state.busy = true;
  const session = state.sessionGeneration;
  document.querySelectorAll('#detail-panel button').forEach((button) => { button.disabled = true; });
  try {
    await request(path, { ...fields, tenant: context.tenant, case_id: context.case_id });
    if (session === state.sessionGeneration) notice(successMessage);
  } catch (error) { if (session === state.sessionGeneration) notice(error.message, true); }
  finally {
    if (session !== state.sessionGeneration) return;
    state.busy = false;
    if (state.authMode === 'demo' || state.accessToken) {
      try { await refresh(); } catch (error) { notice(error.message, true); }
    }
  }
}

function showLogin() {
  state.accessToken = ''; state.token = ''; state.principal = null;
  state.authMode = 'roles'; state.cases = []; state.selected = null; state.detail = null;
  ++state.sessionGeneration; ++state.listGeneration; ++state.detailGeneration;
  state.busy = false;
  clearOperations();
  $('access-token').value = '';
  $('auth-panel').classList.remove('hidden');
  $('inbox-layout').classList.add('hidden');
  $('sign-out').classList.add('hidden');
  $('identity-label').textContent = 'Sign in to your workspace';
  $('mode-label').textContent = 'LOCAL ACCESS CONTROL';
  $('tenant').replaceChildren(); $('tenant').disabled = true;
  $('detail-panel').replaceChildren(); renderCases();
  showView('inbox', false);
}

async function start() {
  const session = state.sessionGeneration;
  try {
    const boot = await request('/api/bootstrap');
    state.authMode = boot.auth_mode || 'demo';
    if (boot.auth_required) { showLogin(); return; }
    state.principal = boot.principal || null;
    state.token = boot.csrf_token;
    if (state.principal) state.tenant = state.principal.tenant;
    $('auth-panel').classList.add('hidden');
    $('inbox-layout').classList.remove('hidden');
    $('tenant').replaceChildren();
    $('tenant').disabled = Boolean(state.principal);
    $('mode-label').textContent = state.principal ? 'LOCAL ACCESS CONTROL' : 'DEMO WORKSPACE';
    $('identity-label').textContent = state.principal ? `${state.principal.subject} · ${human(state.principal.role)}` : 'Unrestricted local demo';
    $('sign-out').classList.toggle('hidden', !state.principal);
    for (const tenant of boot.tenants) {
      const option = node('option', '', tenant.name); option.value = tenant.id; $('tenant').append(option);
    }
    $('tenant').value = state.tenant;
    showView(state.view, false);
    await refresh();
  } catch (error) { if (session === state.sessionGeneration) notice(error.message, true); }
}

$('tenant').addEventListener('change', async () => {
      if (state.busy) { $('tenant').value = state.tenant; return; }
      state.tenant = $('tenant').value; state.selected = null; state.detail = null;
      clearOperations();
      const context = scope();
      state.cases = []; renderCases();
      ++state.detailGeneration;
      $('detail-panel').replaceChildren(node('div', 'empty-state', 'Loading workspace…'));
      notice();
      try { await refresh(); } catch (error) { if (currentScope(context)) notice(error.message, true); }
      if (currentScope(context) && state.view === 'imports') await loadHistory();
});
$('sign-in').addEventListener('submit', async (event) => {
  event.preventDefault();
  state.accessToken = $('access-token').value.trim();
  $('access-token').value = '';
  ++state.sessionGeneration;
  clearOperations();
  notice();
  await start();
});
$('sign-out').addEventListener('click', () => { showLogin(); notice('Signed out. Your access token was cleared from this page.'); });

const CSV_LIMIT = 2 * 1024 * 1024;
const visibleLimit = 200;
const signedIn = () => state.authMode === 'demo' || Boolean(state.accessToken && state.principal);
const scope = () => ({ tenant: state.tenant, session: state.sessionGeneration, generation: state.operationsGeneration });
const currentScope = (context) => context.tenant === state.tenant && context.session === state.sessionGeneration && context.generation === state.operationsGeneration;

function operationStatus(id, text = '', error = false) {
  $(id).textContent = text;
  $(id).classList.toggle('error', error);
}

function clearOperations() {
  ++state.operationsGeneration; ++state.historyGeneration; ++state.batchGeneration;
  ++state.fileGeneration.import; ++state.fileGeneration.reconcile;
  state.importBusy = false; state.reconcileBusy = false;
  for (const id of ['import-text', 'reconcile-text', 'import-file', 'reconcile-file', 'import-batch-key']) $(id).value = '';
  for (const id of ['import-result', 'reconcile-result', 'import-history', 'import-history-detail']) $(id).replaceChildren();
  for (const id of ['import-status', 'reconcile-status', 'history-status']) operationStatus(id);
  for (const id of ['import-submit', 'reconcile-submit', 'ledger-download', 'imports-refresh']) $(id).disabled = false;
}

function showView(view, load = true) {
  if (!['inbox', 'imports', 'reconcile'].includes(view)) return;
  state.view = view;
  const labels = { inbox: ['Support inbox', 'A clear path to resolution.', 'Review the request, check the policy, then take action.'],
    imports: ['Order imports', 'From source files to clear cases.', 'Validate rows, inspect issues and trace every imported order.'],
    reconcile: ['Refund reconciliation', 'Make the records agree.', 'Find differences between a CSV export and the local refund ledger.'] };
  const [label, title, description] = labels[view];
  $('view-label').textContent = label; $('page-title').textContent = title; $('page-description').textContent = description;
  for (const name of ['inbox', 'imports', 'reconcile']) {
    $(`${name}-layout`).classList.toggle('hidden', !signedIn() || name !== view);
    const button = $(`nav-${name}`);
    button.classList.toggle('active', name === view);
    if (name === view) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    button.disabled = !signedIn();
  }
  $('inbox-stats').classList.toggle('hidden', !signedIn() || view !== 'inbox');
  $('import-form').classList.toggle('hidden', !can('operator'));
  $('import-permission').classList.toggle('hidden', can('operator'));
  if (load && signedIn() && view === 'imports') loadHistory();
}

function checkedCSV(value) {
  if (!value.trim()) throw new Error('Choose a CSV file or paste its contents first.');
  if (new TextEncoder().encode(value).length > CSV_LIMIT) throw new Error('CSV exceeds the 2 MiB limit.');
  return value;
}

function csvCell(value) { return `"${String(value ?? '').replaceAll('"', '""')}"`; }

function simpleTable(headers, rows) {
  const wrapper = node('div', 'table-scroll');
  const table = node('table', 'operations-table');
  const head = node('thead'); const heading = node('tr');
  for (const label of headers) { const th = node('th', '', label); th.scope = 'col'; heading.append(th); }
  head.append(heading); table.append(head);
  const body = node('tbody');
  for (const values of rows.slice(0, visibleLimit)) {
    const tr = node('tr');
    for (const value of values) { const td = node('td'); if (value instanceof Node) td.append(value); else td.textContent = String(value ?? ''); tr.append(td); }
    body.append(tr);
  }
  table.append(body); wrapper.append(table);
  if (rows.length > visibleLimit) wrapper.append(node('p', 'field-hint', `Showing the first ${visibleLimit} of ${rows.length} rows. The summary covers every row.`));
  return wrapper;
}

function summaryCards(entries) {
  const cards = node('div', 'result-summary');
  for (const [label, count] of entries) cards.append(append(node('div'), node('span', '', label), node('strong', '', count)));
  return cards;
}

function renderImport(result, target) {
  const context = scope();
  const panel = $(target); panel.replaceChildren();
  panel.append(node('h3', '', `Batch ${result.batch_key}`), summaryCards([
    ['New records', result.accepted], ['Updated', result.updated], ['Unchanged', result.unchanged], ['Quarantined', result.quarantined]]));
  panel.append(node('p', 'hash-note', `Source SHA-256: ${result.batch_hash}`));
  const issues = result.issues || [];
  if (issues.length) {
    panel.append(node('h4', '', 'Rows that need attention'), simpleTable(['CSV line', 'Issue', 'Detail'], issues.map((item) => [item.row, human(item.code), item.message])));
  } else panel.append(node('p', 'field-hint', 'No rows were quarantined.'));
  const records = result.rows || [];
  if (records.length) {
    panel.append(node('h4', '', 'Imported records'));
    const rows = records.slice(0, visibleLimit).map((item) => {
      const open = node('button', 'text-button', item.case_id); open.type = 'button';
      open.addEventListener('click', async () => {
        if (!currentScope(context)) return;
        state.selected = item.case_id; showView('inbox');
        try { await refresh(); } catch (error) { if (currentScope(context)) notice(error.message, true); }
      });
      return [item.row, item.order_id, open, human(item.status)];
    });
    panel.append(simpleTable(['CSV line', 'Order', 'Open case', 'Result'], rows));
    if (records.length > visibleLimit) panel.append(node('p', 'field-hint', `Showing the first ${visibleLimit} of ${records.length} records. The saved batch result and summary include every row.`));
  } else panel.append(node('p', 'field-hint', 'No records were accepted from this batch. Correct the row issues and use a new batch key.'));
}

async function loadHistory() {
  if (!signedIn()) return;
  const context = scope(); const generation = ++state.historyGeneration;
  operationStatus('history-status', 'Loading import history…');
  $('imports-refresh').disabled = true;
  try {
    const response = await request(`/api/imports?tenant=${encodeURIComponent(context.tenant)}`);
    if (!currentScope(context) || generation !== state.historyGeneration) return;
    $('import-history').replaceChildren();
    const batches = response.batches || [];
    if (!batches.length) $('import-history').append(node('p', 'empty-state compact', 'No import batches yet.'));
    else {
      const rows = batches.map((batch) => {
        const button = node('button', 'text-button', batch.batch_key); button.type = 'button';
        button.addEventListener('click', () => loadBatch(batch.batch_key));
        return [button, new Date(batch.created_at).toLocaleString('en-GB'), batch.accepted, batch.updated, batch.unchanged, batch.quarantined];
      });
      $('import-history').append(simpleTable(['Batch', 'Imported', 'New', 'Updated', 'Unchanged', 'Issues'], rows));
    }
    operationStatus('history-status', `${batches.length} saved ${batches.length === 1 ? 'batch' : 'batches'}. Select a batch to inspect its original result.`);
  } catch (error) { if (currentScope(context) && generation === state.historyGeneration) operationStatus('history-status', error.message, true); }
  finally { if (currentScope(context) && generation === state.historyGeneration) $('imports-refresh').disabled = false; }
}

async function loadBatch(key) {
  const context = scope(); const generation = ++state.batchGeneration;
  $('import-history-detail').replaceChildren(node('p', 'operation-status', 'Loading batch details…'));
  try {
    const result = await request(`/api/imports/${encodeURIComponent(key)}?tenant=${encodeURIComponent(context.tenant)}`);
    if (!currentScope(context) || generation !== state.batchGeneration) return;
    renderImport(result, 'import-history-detail');
  } catch (error) {
    if (currentScope(context) && generation === state.batchGeneration) $('import-history-detail').replaceChildren(node('p', 'operation-status error', error.message));
  }
}

function renderReconciliation(result) {
  const groups = [['matched', 'Matched'], ['missing_external', 'Missing from export'], ['duplicate_external', 'Duplicate IDs'],
    ['mismatched', 'Value mismatches'], ['unknown_external', 'Unknown to ledger'], ['invalid_rows', 'Invalid rows']];
  const panel = $('reconcile-result'); panel.replaceChildren();
  panel.append(summaryCards(groups.map(([key, label]) => [label, (result[key] || []).length])));
  panel.append(node('p', 'field-hint', 'Duplicate IDs are reported separately. Missing means an internal refund ID is absent from valid export rows. A match confirms these supplied fields only.'));
  if (result.source_hash) panel.append(node('p', 'hash-note', `Export SHA-256: ${result.source_hash}`));
  for (const [key, label] of groups) {
    const rows = result[key] || [];
    const details = node('details', 'reconciliation-group'); details.open = key !== 'matched' && rows.length > 0;
    details.append(node('summary', '', `${label} (${rows.length})`));
    if (!rows.length) details.append(node('p', 'field-hint', 'No entries in this group.'));
    else if (key === 'mismatched') details.append(simpleTable(['CSV line', 'Refund ID', 'Export order', 'Ledger order', 'Export amount', 'Ledger amount'], rows.map((item) => [
      item.external.row, item.external.external_id, item.external.order_id, item.internal.order_id, money(item.external.amount_pence), money(item.internal.amount_pence)])));
    else if (key === 'duplicate_external') details.append(simpleTable(['Refund ID', 'CSV lines'], rows.map((item) => [item.external_id, (item.rows || []).join(', ')])));
    else if (key === 'invalid_rows') details.append(simpleTable(['CSV line', 'Issue', 'Detail'], rows.map((item) => [item.row, human(item.code), item.message])));
    else details.append(simpleTable(['CSV line', 'Refund ID', 'Order', 'Amount'], rows.map((item) => [item.row ?? 'Local ledger', item.external_id || item.id, item.order_id, money(item.amount_pence)])));
    panel.append(details);
  }
}

for (const kind of ['import', 'reconcile']) {
  $(`${kind}-text`).addEventListener('input', () => { ++state.fileGeneration[kind]; });
  $(`${kind}-file`).addEventListener('change', async () => {
    const context = scope(); const generation = ++state.fileGeneration[kind];
    const file = $(`${kind}-file`).files[0];
    if (!file) return;
    operationStatus(`${kind}-status`, 'Reading CSV file…');
    try {
      if (file.size > CSV_LIMIT) throw new Error('CSV exceeds the 2 MiB limit.');
      const bytes = await file.arrayBuffer();
      let text;
      try { text = new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
      catch { throw new Error('CSV must be valid UTF-8 text. Re-export the file as UTF-8.'); }
      if (!currentScope(context) || generation !== state.fileGeneration[kind]) return;
      checkedCSV(text); $(`${kind}-text`).value = text;
      operationStatus(`${kind}-status`, 'CSV loaded. Review the contents before continuing.');
    } catch (error) { if (currentScope(context) && generation === state.fileGeneration[kind]) operationStatus(`${kind}-status`, error.message, true); }
  });
}

$('import-sample').addEventListener('click', () => {
  if (!can('operator')) return;
  ++state.fileGeneration.import;
  const suffix = crypto.randomUUID().slice(0, 8);
  const date = new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString();
  const rows = [[`sample-order-${suffix}`, `sample-customer-${suffix}`, '7500', date, `sample-case-${suffix}`, 'Fictional unused item returned.'],
    [`sample-order-review-${suffix}`, `sample-customer-review-${suffix}`, '25000', date, `sample-case-review-${suffix}`, 'Fictional damaged delivery. Please review the refund.']];
  $('import-text').value = 'order_id,customer_id,total_pence,purchased_at,case_id,customer_message\n' + rows.map((row) => row.map(csvCell).join(',')).join('\n') + '\n';
  $('import-file').value = ''; $('import-batch-key').value = `synthetic-${suffix}`;
  operationStatus('import-status', 'Two fictional orders prepared. Import them to add cases to this workspace.');
});

$('import-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (state.importBusy || !can('operator') || !signedIn()) return;
  const context = scope();
  try {
    const csv = checkedCSV($('import-text').value);
    const key = $('import-batch-key').value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$/.test(key)) throw new Error('Use a batch key of 1-96 letters, digits, underscores or hyphens, starting with a letter or digit.');
    state.importBusy = true; $('import-submit').disabled = true;
    operationStatus('import-status', 'Validating and importing records…'); $('import-result').replaceChildren();
    const result = await request('/api/import-orders', { tenant: context.tenant, csv_text: csv, batch_key: key });
    if (!currentScope(context)) return;
    renderImport(result, 'import-result'); operationStatus('import-status', 'Batch saved. Review the counts and any quarantined rows below.');
    await loadHistory();
    if (currentScope(context)) await refresh();
  } catch (error) { if (currentScope(context)) operationStatus('import-status', error.message, true); }
  finally { if (currentScope(context)) { state.importBusy = false; $('import-submit').disabled = false; } }
});

$('reconcile-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (state.reconcileBusy || !signedIn()) return;
  const context = scope();
  try {
    const csv = checkedCSV($('reconcile-text').value);
    state.reconcileBusy = true; $('reconcile-submit').disabled = true;
    operationStatus('reconcile-status', 'Comparing the export with the local ledger…'); $('reconcile-result').replaceChildren();
    const result = await request('/api/reconcile-refunds', { tenant: context.tenant, csv_text: csv });
    if (!currentScope(context)) return;
    renderReconciliation(result); operationStatus('reconcile-status', 'Comparison complete. The ledger was not changed.');
  } catch (error) { if (currentScope(context)) operationStatus('reconcile-status', error.message, true); }
  finally { if (currentScope(context)) { state.reconcileBusy = false; $('reconcile-submit').disabled = false; } }
});

$('ledger-download').addEventListener('click', async () => {
  const context = scope(); $('ledger-download').disabled = true;
  operationStatus('reconcile-status', 'Preparing the local ledger export…');
  try {
    const result = await request(`/api/refunds?tenant=${encodeURIComponent(context.tenant)}`);
    if (!currentScope(context)) return;
    if (!result.refunds?.length) { operationStatus('reconcile-status', 'No refunds are recorded in this workspace yet.'); return; }
    const csv = 'external_id,order_id,amount_pence\n' + result.refunds.map((item) => [item.id, item.order_id, item.amount_pence].map(csvCell).join(',')).join('\n') + '\n';
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const link = node('a'); link.href = url; link.download = `casework-${context.tenant}-synthetic-refunds.csv`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    operationStatus('reconcile-status', 'Local synthetic ledger downloaded. No payment provider was contacted.');
  } catch (error) { if (currentScope(context)) operationStatus('reconcile-status', error.message, true); }
  finally { if (currentScope(context)) $('ledger-download').disabled = false; }
});

$('imports-refresh').addEventListener('click', loadHistory);
document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => { notice(); showView(button.dataset.view); }));
start();
