const $ = (id) => document.getElementById(id);
const state = { token: '', tenant: 'demo-shop', cases: [], selected: null, detail: null, busy: false,
  listGeneration: 0, detailGeneration: 0, accessToken: '', principal: null, authMode: 'demo', sessionGeneration: 0 };
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
  for (const item of state.cases) {
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
    suggest.disabled = true; suggest.textContent = 'Drafting suggestion…'; notice();
    try {
      const draft = await request('/api/suggest', { tenant, case_id: caseId });
      if (state.selected !== caseId || state.tenant !== tenant) return;
      action.value = draft.action;
      amount.value = (draft.amount_pence / 100).toFixed(2);
      amount.disabled = draft.action === 'escalate'; amount.required = !amount.disabled;
      reason.value = draft.reason;
      notice('Suggestion drafted. Review the amount and reason, then check the proposal. No action has been taken.');
    } catch (error) { if (state.selected === caseId && state.tenant === tenant) notice(error.message, true); }
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
  document.querySelectorAll('#detail-panel button').forEach((button) => { button.disabled = true; });
  try {
    await request(path, { ...fields, tenant: context.tenant, case_id: context.case_id });
    notice(successMessage);
  } catch (error) { notice(error.message, true); }
  finally {
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
  $('access-token').value = '';
  $('auth-panel').classList.remove('hidden');
  $('inbox-layout').classList.add('hidden');
  $('sign-out').classList.add('hidden');
  $('identity-label').textContent = 'Sign in to your workspace';
  $('mode-label').textContent = 'LOCAL ACCESS CONTROL';
  $('tenant').replaceChildren(); $('tenant').disabled = true;
  $('detail-panel').replaceChildren(); renderCases();
}

async function start() {
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
    await refresh();
  } catch (error) { notice(error.message, true); }
}

$('tenant').addEventListener('change', async () => {
      if (state.busy) { $('tenant').value = state.tenant; return; }
      state.tenant = $('tenant').value; state.selected = null; state.detail = null;
      ++state.detailGeneration;
      $('detail-panel').replaceChildren(node('div', 'empty-state', 'Loading workspace…'));
      notice();
      try { await refresh(); } catch (error) { notice(error.message, true); }
});
$('sign-in').addEventListener('submit', async (event) => {
  event.preventDefault();
  state.accessToken = $('access-token').value.trim();
  $('access-token').value = '';
  ++state.sessionGeneration;
  notice();
  await start();
});
$('sign-out').addEventListener('click', () => { showLogin(); notice('Signed out. Your access token was cleared from this page.'); });

start();
