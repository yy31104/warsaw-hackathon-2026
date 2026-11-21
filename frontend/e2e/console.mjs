import assert from 'node:assert/strict';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {join, resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {chromium} from 'playwright';

function local(name) {
  const url = new URL(process.env[name]);
  assert(url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname),
    `${name} must name an isolated loopback service`);
  return url.origin;
}

const ui = local('CONSOLE_TEST_UI');
const api = local('CONSOLE_TEST_API');
const telecom = local('CONSOLE_TEST_TELECOM');
assert(new Set([ui, api, telecom]).size === 3, 'Use distinct UI, API and fake telecom ports.');
const output = resolve(process.env.CONSOLE_TEST_OUTPUT ?? 'evidence/console');
const tag = Date.now().toString(36);
const deadlineMs = 18000;
const ledger = new Map();
const unexpected = [];
const pageErrors = [];
const results = {};
const screenshots = [];
const measured = {};

async function json(path, {body, actor, method, headers = {}, origin = api} = {}) {
  const response = await fetch(origin + path, {
    signal: AbortSignal.timeout(deadlineMs),
    method: method ?? (body !== undefined ? 'POST' : 'GET'),
    headers: {...(actor ? {'X-User': actor} : {}), ...(body !== undefined ? {'Content-Type': 'application/json'} : {}), ...headers},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await response.text();
  assert(response.ok, `${method ?? 'request'} ${path} returned ${response.status} ${text}`);
  return JSON.parse(text);
}

async function until(description, predicate) {
  const deadline = Date.now() + deadlineMs;
  while (Date.now() < deadline) {
    const result = await predicate();
    if (result) return result;
    await delay(120);
  }
  assert.fail('Timed out waiting for ' + description);
}

async function test(label, action) {
  const start = performance.now();
  try {
    await action();
    results[label] = {status: 'PASS', seconds: Number(((performance.now() - start) / 1000).toFixed(2))};
    console.log(`PASS ${label} · ${results[label].seconds}s`);
  } catch (error) {
    results[label] = {status: 'FAIL', reason: String(error)};
    throw error;
  }
}

async function ingest(event, suffix) {
  return json('/v1/events/generic-webhook', {body: event,
    headers: {'X-Api-Key': 'demo-generic-key', 'Idempotency-Key': tag + '-' + suffix}});
}

async function seedRule(rule) {
  return json('/v1/rules', {actor: rule.actor, body: rule.spec});
}

const main = page => page.getByRole('main');
const button = (page, label) => main(page).getByRole('button', {name: label, exact: true});
const visible = locator => locator.waitFor({state: 'visible', timeout: deadlineMs});
const stable = async page => {
  await visible(page.getByText('AI off · rules only', {exact: true}));
  assert.equal(await page.locator('.mock-banner').count(), 0);
};
const navigate = (page, label) => page.getByRole('navigation', {name: 'Main navigation'})
  .getByRole('link', {name: new RegExp('^' + label)}).click();

function observe(page) {
  page.on('pageerror', error => pageErrors.push(String(error)));
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.origin !== ui || url.pathname.startsWith('/fixtures/') || url.pathname.includes('/mock/')) {
      unexpected.push(request.url());
    }
  });
  page.on('response', response => {
    const url = new URL(response.url());
    if (!url.pathname.startsWith('/v1/') && !['/ops-health', '/metrics'].includes(url.pathname)) return;
    const key = `${response.request().method()} ${url.pathname}${url.search} → ${response.status()}`;
    ledger.set(key, (ledger.get(key) ?? 0) + 1);
  });
  return page;
}

async function chooseActor(page, actor) {
  await visible(page.getByLabel('Viewing as', {exact: true}));
  await page.getByLabel('Viewing as', {exact: true}).selectOption(actor);
}

const browser = await chromium.launch();
const contexts = [];
async function context(options = {}) {
  const session = await browser.newContext({viewport: {width: 1440, height: 900}, reducedMotion: 'reduce', ...options});
  contexts.push(session);
  return session;
}

const settlementId = 'console-settlement-' + tag;
const sourceRule = 'console-clearing-' + tag;
const ruleForEdit = 'console-review-' + tag;
const settlementTitle = 'Settlement workflow timed out';
const pendingTitle = 'Retry wf-8812';
const flow = () => ({
  type: 'WORKFLOW_FAILED', workflowId: 'wf-8812', workflowName: 'console-' + tag, step: 'validate-counterparty',
  severity: 'FATAL', timestamp: new Date().toISOString(), environment: 'prod',
  error: {code: 'CONSOLE_TIMEOUT', message: 'Counterparty service timed out'},
});
const referencePath = 'ref_' + tag;
const priorityPath = 'priority_' + tag;
const vendor = {vendor_feed: 'clearing-' + tag, [priorityPath]: 'P1', amount_minor: 250_000_000, [referencePath]: 'T-55102'};
const schemaName = 'Clearing trade messages';
const shape = Object.keys(vendor).sort().join(',');
let incident;
let proofSource;

async function openSource(page, fingerprint = '') {
  await page.goto(ui + '/sources' + (fingerprint ? '?fingerprint=' + encodeURIComponent(fingerprint) : ''));
  await chooseActor(page, 'ops');
  await stable(page);
  await visible(page.getByRole('combobox', {name: 'Review issue'}));
  if (fingerprint) await page.getByRole('combobox', {name: 'Review issue'}).selectOption(fingerprint);
  await visible(page.getByLabel('Raw sample'));
  const suggestion = page.waitForResponse(response => new URL(response.url()).pathname === '/v1/schemas/suggest');
  await button(page, 'Suggest mapping').click();
  await visible(page.getByLabel('Map ' + priorityPath, {exact: true}));
  return (await (await suggestion).json()).draft.id;
}

async function completeSource(page) {
  const severity = page.getByLabel('Map ' + priorityPath, {exact: true});
  await severity.selectOption('severity');
  await page.getByLabel('Severity for P1', {exact: true}).selectOption('warning');
  const amount = main(page).locator('.cf-map-row').filter({has: page.getByLabel('Map amount_minor', {exact: true})});
  const details = amount.locator('details');
  if (!(await details.evaluate(element => element.open))) await amount.getByText('Set units', {exact: true}).click();
  await page.getByLabel('Unit for amount_minor', {exact: true}).fill('EUR');
  await page.getByLabel('Scale for amount_minor', {exact: true}).fill('0.01');
  await page.getByRole('checkbox', {name: 'Email', exact: true}).check();
  await page.getByRole('checkbox', {name: 'UI', exact: true}).check();
  await main(page).getByText('Format detection', {exact: true}).click();
  await button(page, 'Match sample shape').click();
  await main(page).getByText('Format detection', {exact: true}).click();
}

async function takeShot(page, variant, label) {
  const width = page.viewportSize().width;
  await page.evaluate(() => document.fonts.ready);
  if (width !== 390) await page.evaluate(() => window.scrollTo(0, 0));
  const actual = await page.evaluate(() => ({
    width: document.documentElement.scrollWidth,
    nav: getComputedStyle(document.querySelector('.cf-rail')).position,
    scheme: document.documentElement.dataset.mantineColorScheme,
  }));
  assert(actual.width <= width, `${variant}/${label} overflowed horizontally (${actual.width} > ${width})`);
  assert.equal(actual.nav, width === 390 ? 'fixed' : 'sticky');
  assert.equal(actual.scheme, variant.split('-')[1]);
  const directory = join(output, variant);
  await mkdir(directory, {recursive: true});
  await page.screenshot({path: join(directory, label + '.jpg'), fullPage: width !== 390 && label !== 'intro',
    type: 'jpeg', quality: 82, animations: 'disabled'});
  screenshots.push(`${variant}/${label}.jpg`);
}

try {
  await mkdir(output, {recursive: true});
  assert.deepEqual(await json('/health'), {status: 'ok'});
  assert.equal((await json('/v1/assist/status')).enabled, false);
  const baseline = await json('/v1/alerts?team=settlements&status=open');
  assert(baseline.items.length === 0, 'Start with a fresh disposable Compose project for the browser checks.');
  await seedRule({actor: 'lee', spec: {
    id: settlementId, name: 'Settlement timeouts', owner: 'team:settlements', enabled: true, escalate_after: '20m',
    match: {workflowName: 'console-' + tag}, route: [{to: 'oncall:settlements', channels: ['phone', 'sms']}],
    present: {title: settlementTitle, fields: ['workflowId', 'error.code']}, group: {by: ['workflowId'], mode: 'immediate', cooldown: '10m'},
  }});
  await json('/v1/runbooks', {actor: 'lee', body: {
    id: 'console-retry-' + tag, owner: 'team:settlements', rules: [settlementId], format: 'yaml-steps',
    content: 'steps:\n  - text: "Retry {{workflowId}}"\n    action: retry_workflow\n    workflowId: "{{workflowId}}"\n    approval: required\n    resolves_alert: true\n  - text: Check settlement status manually',
  }});
  const sam = observe(await (await context()).newPage());
  const lee = observe(await (await context()).newPage());

  await test('A · live critical incident and acknowledge', async () => {
    await sam.goto(ui);
    await sam.getByRole('button', {name: 'Open console', exact: true}).click();
    await stable(sam);
    await visible(sam.locator('.cf-clock i[data-live="true"]'));
    const first = await ingest(flow(), 'settlement');
    assert.equal(first.accepted, 1);
    await visible(sam.locator('.cf-hero').getByRole('heading', {name: settlementTitle, exact: true}));
    await visible(sam.locator('.cf-hero').getByText('Lee is paged', {exact: true}));
    incident = await until('settlement incident', async () => (await json('/v1/alerts?team=settlements&status=open')).items
      .find(row => row.rule_id === settlementId));
    assert.equal(incident.severity, 'critical');
    const escalation = (await json(`/v1/alerts/${incident.id}`)).next_escalation;
    assert.equal(escalation.targets[0], 'lee');
    const time = new Intl.DateTimeFormat('en-GB', {timeZone: 'Europe/Warsaw', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'})
      .format(new Date(escalation.at));
    const lede = sam.locator('.cf-hero .cf-lede');
    await visible(lede.getByText(time, {exact: true}));
    assert.equal(await lede.innerText(), `Lee is paged at ${time} unless someone acknowledges first.`);
    assert.equal(await lede.locator('time').getAttribute('datetime'), escalation.at);
    await ingest(flow(), 'settlement-repeat');
    await visible(sam.locator('.cf-hero').getByText('×2', {exact: true}));
    const sent = await until('Sam SMS and call', async () => {
      const {items} = await json(`/v1/alerts/${incident.id}/notifications`);
      const deliveries = items.filter(item => item.recipient === 'sam'
        && ['sms', 'phone'].includes(item.channel) && item.state === 'delivered');
      return ['sms', 'phone'].every(channel => deliveries.some(item => item.channel === channel)) ? deliveries : null;
    });
    const tele = await json('/messages?to=' + encodeURIComponent('+15550100002'), {origin: telecom});
    const notificationIds = new Set(sent.map(item => String(item.id)));
    const receipts = tele.items.filter(item => notificationIds.has(String(item.idempotency_key)));
    assert.deepEqual(receipts.map(item => item.channel).sort(), ['phone', 'sms']);
    assert.ok(receipts.every(item => /\/a\/(\d+)(?:\s|$)/.exec(item.text)?.[1] === String(incident.id)));
    await sam.locator('.cf-hero').getByRole('button', {name: 'Acknowledge'}).click();
    await visible(main(sam).getByText('Escalation stopped', {exact: true}));
    await visible(main(sam).getByText('Acknowledged, so escalation has stopped. It stays open until someone resolves it.', {exact: true}));
    const detail = await json(`/v1/alerts/${incident.id}`);
    assert.equal(detail.status, 'acknowledged');
    assert.equal(detail.acked_by, 'sam');
    assert.equal(detail.next_escalation, null);
    measured.incident = {alert_id: incident.id, event_id: first.event_ids[0], occurrence_count: detail.occurrence_count,
      provider_channels: receipts.map(item => item.channel).sort(), acked_by: detail.acked_by, next_escalation: detail.next_escalation};
  });

  await test('B · Lee approves persisted retry in a separate context', async () => {
    await lee.goto(ui);
    await lee.getByRole('button', {name: 'Open console', exact: true}).click();
    await chooseActor(lee, 'lee');
    await stable(lee);
    const step = sam.locator('#runbook .cf-step').filter({hasText: pendingTitle});
    await step.getByRole('button', {name: 'Request approval'}).click();
    await visible(step.getByText('Waiting for Lee', {exact: true}));
    const pending = (await json(`/v1/alerts/${incident.id}`)).pending_actions;
    assert.equal(pending.length, 1);
    assert.equal(pending[0].requested_by, 'sam');
    await main(lee).getByRole('link', {name: 'Review', exact: true}).click();
    await lee.reload();
    await stable(lee);
    const approve = lee.locator('#runbook .cf-step').filter({hasText: pendingTitle}).getByRole('button', {name: 'Approve', exact: true});
    await approve.click();
    await visible(step.getByText(/Approved by Lee/));
    await visible(step.getByText('Succeeded', {exact: true}));
    const server = await json(`/v1/alerts/${incident.id}/runbook`);
    const actual = server.steps.find(item => item.run_id === pending[0].run_id);
    assert.equal(actual.state, 'succeeded');
    assert.equal(actual.approved_by, 'lee');
    assert.equal((await json(`/v1/alerts/${incident.id}`)).status, 'resolved');
    measured.approval = {run_id: actual.run_id, state: actual.state, approved_by: actual.approved_by};
  });

  await test('Timeline · one plain row per delivery and raw history in More details', async () => {
    const timeline = main(sam).locator('.cf-timeline');
    await visible(timeline.getByText('Opened by rule Settlement timeouts', {exact: true}));
    const {items} = await json(`/v1/alerts/${incident.id}/notifications`);
    const call = items.find(item => item.channel === 'phone' && item.recipient === 'sam' && item.state === 'delivered');
    const latency = call.attempt_log.find(attempt => attempt.outcome === 'delivered').latency_ms;
    const callText = `Call to Sam · delivered in ${(latency / 1000).toFixed(2)} s`;
    await visible(timeline.getByText(callText, {exact: true}));
    await visible(timeline.getByText('SMS to Sam · delivered', {exact: true}));
    assert.equal(await timeline.getByText(/^Call to Sam/).count(), 1);
    assert.equal(await timeline.getByText(/^SMS to Sam/).count(), 1);
    assert.doesNotMatch(await timeline.innerText(), /NEW_ALERT|SUPPRESSED_COOLDOWN|console-settlement|\(attempt \d+/);
    await main(sam).getByRole('link', {name: 'More details', exact: true}).click();
    await visible(main(sam).getByText(settlementId + ' → NEW_ALERT', {exact: true}));
    await visible(main(sam).getByText('phone → sam: delivered (attempt 1)', {exact: true}));
    measured.timeline = {call, label: callText, sms: 'SMS to Sam · delivered'};
  });

  const ops = observe(await (await context()).newPage());
  await test('C and F · unknown JSON verified and routed with assist off', async () => {
    await seedRule({actor: 'rita', spec: {
      id: sourceRule, owner: 'team:rates', enabled: true,
      match: {all: [{field: 'vendor_feed', op: 'glob', value: 'clearing-' + tag + '*'},
        {field: '$severity', op: 'eq', value: 'warning'}, {field: 'amount_minor', op: 'gt', value: 1_000_000}]},
      route: [{to: 'user:rita', channels: ['email']}, {to: 'team:rates', channels: ['ui']}],
      present: {title: 'Clearing event verified', fields: ['amount_minor']},
      group: {by: [referencePath], mode: 'immediate', cooldown: '10m'},
    }});
    const received = await ingest(vendor, 'new-format');
    const unseen = await json(`/v1/events/${received.event_ids[0]}`);
    assert.equal(unseen.status, 'needs_review');
    const schemaId = await openSource(ops, shape);
    const positive = ops.getByLabel('Should alert', {exact: true});
    const quiet = ops.getByLabel('Should stay quiet', {exact: true});
    const name = ops.getByLabel('Schema name', {exact: true});
    assert.equal(await quiet.inputValue(), await positive.inputValue());
    assert.equal(await name.inputValue(), '');
    assert.equal(await ops.getByLabel('Schema ID', {exact: true}).count(), 0);
    assert.equal(await button(ops, 'Run test').isEnabled(), false);
    await name.fill(schemaName);
    const recipients = ops.getByRole('combobox', {name: 'Expected recipients', exact: true});
    await recipients.selectOption('rita');
    await recipients.selectOption('team:rates');
    await visible(button(ops, 'Remove Rita'));
    await visible(button(ops, 'Remove Rates team'));
    await button(ops, 'Run test').click();
    await visible(main(ops).getByText('Fail', {exact: true}).first());
    assert.equal(await button(ops, 'Go live').isEnabled(), false);
    await completeSource(ops);
    await button(ops, 'Run test').click();
    const edit = button(ops, 'Edit amount_minor');
    await visible(edit);
    const quietBox = main(ops).locator('.cf-sample-box').filter({has: quiet});
    await visible(quietBox.getByText('Fail', {exact: true}));
    assert.equal(await button(ops, 'Go live').isEnabled(), false);
    await edit.click();
    assert.equal(await quiet.evaluate(input => input.value.slice(input.selectionStart, input.selectionEnd)), '250000000');
    await takeShot(ops, 'desktop-light', 'sources-quiet-test');
    await ops.keyboard.insertText('10000000');
    assert.deepEqual(JSON.parse(await quiet.inputValue()), {...vendor, amount_minor: 10_000_000});
    assert.deepEqual(JSON.parse(await positive.inputValue()), vendor);
    await button(ops, 'Run test').click();
    await until('both samples pass', async () => await main(ops).getByText('Pass', {exact: true}).count() === 2);
    await visible(main(ops).locator('.cf-result').getByText('amount_minor', {exact: true}).first());
    const publication = ops.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/v1/schemas');
    await button(ops, 'Go live').click();
    const response = await publication;
    assert(response.ok(), await response.text());
    assert.equal(response.request().postDataJSON().name, schemaName);
    assert.equal(response.request().postDataJSON().id, schemaId);
    const published = await response.json();
    assert(published.reprocessed >= 1);
    await visible(main(ops).getByText(published.reprocessed + ' waiting events re-checked', {exact: true}));
    const sent = await ingest({...vendor, [referencePath]: 'T-55104'}, 'new-format-live');
    const next = await until('processed next source message', async () => {
      const row = await json(`/v1/events/${sent.event_ids[0]}`);
      return row.status === 'processed' ? row : null;
    });
    assert.equal(next.fields.amount_minor, 2_500_000);
    const explain = await json(`/v1/events/${next.id}/explain`);
    assert(explain.decisions.some(item => item.rule_id === sourceRule && item.reason.matched));
    assert(explain.notifications.some(item => item.recipient === 'rita' && item.channel === 'email'));
    measured.source = {unknown_event_id: unseen.id, next_event_id: next.id, schema: next.schema_id,
      schema_name: schemaName, deciding_field: 'amount_minor', expected_recipients: ['rita', 'team:rates'], reprocessed: published.reprocessed,
      amount: next.fields.amount_minor, delivered_to: 'rita', channel: 'email'};
  });

  await test('D · rule typo suggestion and versioned publish', async () => {
    await seedRule({actor: 'sam', spec: {
      id: ruleForEdit, owner: 'user:sam', enabled: true, match: {all: [
        {field: '$schema', op: 'eq', value: 'WORKFLOW_FAILED'}, {field: 'error.code', op: 'eq', value: 'CONSOLE_IRRELEVANT'},
      ]}, route: [{to: 'user:sam', channels: ['ui']}],
    }});
    await navigate(sam, 'Rules');
    await main(sam).getByRole('button', {name: new RegExp('^' + ruleForEdit)}).click();
    await sam.getByLabel('Alert title', {exact: true}).fill('Reviewed settlement rule');
    await main(sam).getByLabel('Field', {exact: true}).nth(1).fill('errorCode');
    await button(sam, 'Run check').click();
    await button(sam, 'Did you mean error.code').click();
    assert.equal(await main(sam).getByLabel('Field', {exact: true}).nth(1).inputValue(), 'error.code');
    await button(sam, 'Preview impact').click();
    await visible(main(sam).getByText(/Replay · N=/));
    await button(sam, 'Publish v2').click();
    await visible(sam.getByRole('status').getByText(/Published/));
    const saved = (await json('/v1/rules')).items.find(item => item.id === ruleForEdit);
    assert.equal(saved.version, 2);
    assert.equal(saved.match.all[1].field, 'error.code');
    assert.equal(saved.present.title, 'Reviewed settlement rule');
  });

  await test('E · structured runbook validation blocks publish until fixed', async () => {
    await navigate(sam, 'Runbooks');
    await button(sam, 'Create runbook').click();
    await sam.getByLabel('Runbook ID', {exact: true}).fill('console-lint-' + tag);
    await sam.getByLabel('Original text', {exact: true}).fill('1. Restart service settlement-engine\n2. If the restart does not help, check the dashboard.');
    await button(sam, 'Turn into steps').click();
    await visible(main(sam).getByText('Rules draft', {exact: true}));
    await main(sam).getByText('Edit steps', {exact: true}).click();
    const structured = sam.getByLabel('Steps YAML', {exact: true});
    const broken = 'steps:\n  - action: restart_service\n    service: settlement-engine\n    approval: none\n  - text: Check the dashboard';
    await structured.fill(broken);
    await visible(main(sam).getByText('This action requires approval.', {exact: true}));
    assert.equal(await button(sam, 'Publish').isEnabled(), false);
    await structured.fill(broken.replace('approval: none', 'approval: required'));
    await button(sam, 'Publish').click();
    await visible(sam.getByRole('status').getByText(/Published console-lint/));
    const books = await json('/v1/runbooks');
    assert(books.items.some(book => book.id === 'console-lint-' + tag && book.content.includes('approval: required')));
  });

  await test('On call · preference sliders persist', async () => {
    await navigate(sam, 'On call');
    await chooseActor(sam, 'anna');
    await visible(main(sam).getByRole('heading', {name: 'Anna’s day'}));
    const slider = sam.getByRole('slider', {name: /SMS from/});
    const before = Number(await slider.inputValue());
    await slider.press('ArrowRight');
    assert.equal(Number(await slider.inputValue()), before + 1);
    await button(sam, 'Save preferences').click();
    await visible(sam.getByRole('status').getByText('Preferences saved', {exact: true}));
    const actual = await json('/v1/users/anna/preferences', {actor: 'anna'});
    const start = actual.find(row => row.channels.includes('sms')).when.split('-')[0];
    const [hours, minutes] = start.split(':').map(Number);
    assert.equal(hours * 60 + minutes, before + 1);
  });

  await test('Health · live counters and dated benchmark provenance', async () => {
    await navigate(sam, 'Health');
    await visible(main(sam).getByText('API healthy', {exact: true}));
    await visible(main(sam).getByText(/processed · total/));
    const lookup = (await json('/v1/stats/funnel?window=24h')).raw;
    assert.equal((await main(sam).locator('.cf-pipeline dd').first().textContent()).replaceAll(',', ''), String(lookup));
    const percentiles = (await jsonBenchmark('one-normal/summary.json')).receiver.e2e;
    await visible(main(sam).getByText(`${percentiles.p50_ms} ms`, {exact: true}));
    await visible(main(sam).getByText(new RegExp('benchmark · 20 Nov · N=' + percentiles.n)));
    await visible(main(sam).getByText('24/24 delivered after recovery', {exact: true}));
  });

  await test('Now · one fallback tile or four live response tiles', async () => {
    const screen = await context();
    const page = observe(await screen.newPage());
    const path = '/v1/stats/response';
    const received = page.waitForResponse(response => new URL(response.url()).pathname === path);
    await page.goto(ui);
    await page.getByRole('button', {name: 'Open console', exact: true}).click();
    await stable(page);
    const response = await received;
    const group = main(page).getByRole('group', {name: 'Response metrics', exact: true});
    let stats;
    if (response.status() === 404) {
      await visible(group.getByText('More metrics appear after the first incidents.', {exact: true}));
      assert.equal(await group.getByRole('heading', {level: 2}).count(), 1);
    } else {
      assert(response.ok(), await response.text());
      stats = await response.json();
      await visible(group.getByRole('heading', {name: 'Notifications per incident', exact: true}));
      const keys = ['tta_p50_s', 'mtta_s', 'mttr_s', 'notifications_per_alert'];
      assert.deepEqual(await group.locator('.cf-big').allTextContents(), keys.map((key, index) =>
        stats[key] == null ? '—' : stats[key].toLocaleString('en-GB', {maximumFractionDigits: 3}) + (index < 3 ? ' s' : '')));
      await takeShot(page, 'desktop-light', 'now-response-live');
    }
    measured.response_metrics = {api_status: response.status(), tiles: response.ok() ? 4 : 1, stats};
    await screen.close();
  });

  await test('Now · four tiles with an example response and a missing sample', async () => {
    // A separate browser context supplies only this optional endpoint so the live checks stay unmocked.
    const screen = await context();
    const page = await screen.newPage();
    await page.route(url => url.pathname === '/v1/stats/response', route => route.fulfill({json: {
      n_alerts: 4, tta_p50_s: 0.31, mtta_s: 2.25, mttr_s: null, notifications_per_alert: 1.75,
    }}));
    await page.goto(ui);
    await page.getByRole('button', {name: 'Open console', exact: true}).click();
    const group = main(page).getByRole('group', {name: 'Response metrics', exact: true});
    await visible(group.getByText('No samples', {exact: true}));
    assert.deepEqual(await group.getByRole('heading', {level: 2}).allTextContents(),
      ['Time to first notification', 'Time to acknowledge', 'Time to resolve', 'Notifications per incident']);
    assert.deepEqual(await group.locator('.cf-big').allTextContents(), ['0.31 s', '2.25 s', '—', '1.75']);
    await takeShot(page, 'desktop-light', 'now-response-example');
    await screen.close();
  });

  await test('Intro · auto-enter and session replay', async () => {
    const fullMotion = await context({reducedMotion: 'no-preference'});
    const page = observe(await fullMotion.newPage());
    await page.goto(ui);
    await visible(page.getByRole('dialog').getByText('Example', {exact: true}));
    await page.getByRole('dialog').waitFor({state: 'hidden', timeout: deadlineMs});
    await page.reload();
    await stable(page);
    assert.equal(await page.getByRole('dialog').count(), 0);
    await page.getByRole('button', {name: 'Intro', exact: true}).click();
    await visible(page.getByRole('dialog').getByText('Example', {exact: true}));
    await page.getByRole('button', {name: 'Skip', exact: true}).click();
    assert.equal(await page.getByRole('dialog').count(), 0);
    await fullMotion.close();
  });

  await test('Screenshots · four layouts across eight views', async () => {
    const current = {...flow(), workflowId: 'wf-8813', timestamp: new Date().toISOString()};
    await ingest(current, 'screenshot-alert');
    const screenshotIncident = await until('screenshot incident', async () => (await json('/v1/alerts?team=settlements&status=open')).items
      .find(row => row.rule_id === settlementId));
    // A changed field set yields a second genuine review item after the first mapping is confirmed.
    const sample = {sourceSystem: 'clearing-gw', eventRef: 'T-55105',
      message: {amountMinor: 250_000_000, priority: 'P1', result: 'REJECTED'}};
    proofSource = Object.keys(sample).sort().join(',');
    await ingest(sample, 'screenshot-source');
    for (const [view, width, height] of [['desktop', 1440, 900], ['phone', 390, 844]]) {
      for (const color of ['light', 'dark']) {
        const layout = view + '-' + color;
        const screen = await context({viewport: {width, height}});
        await screen.addInitScript(scheme => localStorage.setItem('mantine-color-scheme-value', scheme), color);
        const page = observe(await screen.newPage());
        await page.goto(ui);
        await visible(page.getByRole('dialog').getByText('Example', {exact: true}));
        await takeShot(page, layout, 'intro');
        await page.getByRole('button', {name: 'Open console', exact: true}).click();
        await stable(page);
        await visible(page.locator('.cf-hero').getByText(settlementTitle, {exact: true}));
        await takeShot(page, layout, 'now');
        await page.goto(ui + '/a/' + screenshotIncident.id);
        await visible(page.locator('#runbook').getByText('Retry wf-8813', {exact: true}));
        await visible(main(page).locator('.cf-timeline').getByText('Opened by rule Settlement timeouts', {exact: true}));
        await visible(main(page).locator('.cf-timeline').getByText(/^Call to Sam · delivered in /));
        await visible(main(page).locator('.cf-timeline').getByText('SMS to Sam · delivered', {exact: true}));
        await takeShot(page, layout, 'incident');
        await page.goto(ui + '/sources?fingerprint=' + encodeURIComponent(proofSource));
        await chooseActor(page, 'ops');
        await visible(page.getByLabel('Raw sample'));
        await button(page, 'Suggest mapping').click();
        await visible(page.getByLabel('Map message.priority', {exact: true}));
        await takeShot(page, layout, 'sources');
        await chooseActor(page, 'sam');
        await navigate(page, 'Rules');
        await main(page).getByRole('button', {name: new RegExp('^' + ruleForEdit)}).click();
        await takeShot(page, layout, 'rules');
        await navigate(page, 'Runbooks');
        await visible(main(page).getByRole('heading', {name: 'As steps'}));
        await takeShot(page, layout, 'runbooks');
        await navigate(page, 'On call');
        await chooseActor(page, 'anna');
        await visible(main(page).getByRole('slider', {name: /SMS from/}));
        await takeShot(page, layout, 'on-call');
        await navigate(page, 'Health');
        await visible(main(page).getByText('API healthy', {exact: true}));
        await takeShot(page, layout, 'health');
        await screen.close();
      }
    }
    assert.equal(screenshots.length, measured.response_metrics.api_status === 404 ? 34 : 35);
  });

  await test('Network · genuine API, no fixtures or browser errors', async () => {
    assert.deepEqual(unexpected, []);
    assert.deepEqual(pageErrors, []);
    assert([...ledger.keys()].some(path => path.includes('/v1/stream?team=settlements')));
    assert([...ledger.keys()].some(path => path.includes(`/v1/action-runs/${measured.approval.run_id}/approve → 200`)));
    assert([...ledger.keys()].some(path => path.includes('/v1/schemas/verify → 200')));
    const details = await json(`/v1/alerts/${incident.id}`);
    const explainCalls = [...ledger.keys()].filter(path => /GET \/v1\/events\/\d+\/explain/.test(path));
    if (details.representative_event_id == null) {
      assert.deepEqual(explainCalls, [], 'Hide Why you when the API omits representative_event_id.');
    } else {
      assert(explainCalls.some(path => path.includes(`/v1/events/${details.representative_event_id}/explain`)));
    }
  });
} finally {
  await browser.close();
  await writeFile(join(output, 'result.json'), JSON.stringify({
    source: 'Isolated Compose, production frontend VITE_MOCK=0', when: new Date().toISOString(),
    tests: results, measurements: measured, screenshots, api: Object.fromEntries([...ledger.entries()].sort()),
    unexpected, pageErrors,
  }, null, 2) + '\n');
}

async function jsonBenchmark(path) {
  return JSON.parse(await readFile(new URL('../../loadtest/results/platform-bench/' + path, import.meta.url), 'utf8'));
}
