import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const {chromium} = require('playwright');
const runCommand = promisify(execFile);
const root = fileURLToPath(new URL('../', import.meta.url));
const timeout = 20_000;
const title = 'Settlement failed at validate-counterparty: TIMEOUT';
const samPhone = '+15550100002';

function localUrl(name, protocols) {
  const value = new URL(process.env[name]);
  assert(protocols.includes(value.protocol) && ['127.0.0.1', 'localhost', '[::1]'].includes(value.hostname),
    `${name} must explicitly target an isolated loopback service`);
  return value;
}

async function json(url, options = {}) {
  const response = await fetch(url, {...options, signal: AbortSignal.timeout(timeout)});
  assert(response.ok, `${options.method ?? 'GET'} ${url} returned ${response.status}`);
  return response.json();
}

async function until(description, predicate) {
  const deadline = Date.now() + timeout;
  do {
    const result = await predicate();
    if (result) return result;
    await delay(100);
  } while (Date.now() < deadline);
  assert.fail(`Timed out waiting for ${description}`);
}

async function observeFeed(context, frontend, team, actor) {
  const page = await context.newPage();
  const messages = [];
  const lists = [];
  const errors = [];
  const requests = [];
  const eventSources = new Map();
  let navigations = 0;
  const cdp = await context.newCDPSession(page);
  await cdp.send('Network.enable');
  cdp.on('Network.requestWillBeSent', ({requestId, request}) => {
    const url = new URL(request.url);
    if (url.pathname === '/v1/stream') eventSources.set(requestId, url.searchParams.get('team'));
  });
  cdp.on('Network.eventSourceMessageReceived', ({requestId, eventName, data}) => {
    if (data) messages.push({team: eventSources.get(requestId), type: eventName, data: JSON.parse(data)});
  });
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (['/v1/stream', '/v1/alerts'].includes(url.pathname)) {
      requests.push({path: url.pathname, team: url.searchParams.get('team')});
    }
  });
  page.on('framenavigated', frame => {
    if (frame === page.mainFrame()) navigations++;
  });
  page.on('response', async response => {
    const url = new URL(response.url());
    if (response.request().method() !== 'GET' || url.pathname !== '/v1/alerts') return;
    const list = {team: url.searchParams.get('team'), status: response.status(), items: null};
    lists.push(list);
    if (response.ok()) {
      try {
        list.items = (await response.json()).items.map(({id, team_id}) => ({id, team_id}));
      } catch (error) {
        errors.push(`Alert list JSON: ${error.message}`);
      }
    }
  });
  await page.goto(new URL('/alerts', frontend).href);
  await page.getByText('API', {exact: true}).waitFor();
  await selectOption(page, 'Acting as · simulated auth', actor);
  await until(`${team} scoped alert list`, () => lists.some(row => row.team === team && row.items));
  await until(`${team} scoped stream`, () => messages.some(row => row.team === team && row.type === 'stats.tick'));
  assert.equal(await page.getByRole('textbox', {name: 'Filter by team'}).inputValue(), team);
  return {page, team, messages, lists, errors, requests, navigations: () => navigations};
}

async function selectOption(page, label, value) {
  const field = page.getByRole('textbox', {name: label, exact: true});
  if (await field.inputValue() === value) return;
  await field.click();
  await page.getByRole('option', {name: value, exact: true}).click();
}

function assertScopedLists(lists, team) {
  assert(lists.length > 0, `No ${team} alert list refresh was observed`);
  for (const list of lists) {
    assert.equal(list.status, 200);
    assert.equal(list.team, team, 'A filtered feed requested another team or an unscoped list');
    assert(list.items?.every(item => item.team_id === team), `Foreign alert payload in ${team} list`);
  }
}

async function saveScreenshot(page, output, filename) {
  await page.screenshot({path: resolve(output, filename), fullPage: true, animations: 'disabled'});
  return filename;
}

async function settlementG1({context, frontend, api, telecom, output, run}) {
  const priorAlerts = (await json(new URL('/v1/alerts?team=settlements', api))).items;
  assert(!priorAlerts.some(alert => alert.title === title), 'Use a fresh stack for the canonical G1 fixture');
  assert.equal((await json(new URL('/mode', telecom))).mode, 'ok', 'Start the fake telecom in ok mode');
  const receiptsUrl = new URL(`/messages?to=${encodeURIComponent(samPhone)}`, telecom);
  const priorReceipts = new Set((await json(receiptsUrl)).items.map(item => item.idempotency_key));

  const [settlements, pricing] = await Promise.all([
    observeFeed(context, frontend, 'settlements', 'sam'),
    observeFeed(context, frontend, 'pricing', 'pat'),
  ]);
  const settlementCard = settlements.page.getByRole('link').filter({hasText: title});
  const pricingCard = pricing.page.getByRole('link').filter({hasText: title});
  assert.equal(await settlementCard.count(), 0);
  assert.equal(await pricingCard.count(), 0);
  const settledNavigationCount = settlements.navigations();
  const pricingListOffset = pricing.lists.length;
  const pricingStreamOffset = pricing.messages.length;
  const pricingRequestOffset = pricing.requests.length;
  const settlementListOffset = settlements.lists.length;

  const messageId = `web-g1-${run}`;
  const publisherArgs = ['tools/publish.py', '--via', 'amqp', '--source', 'workflow-bus',
    '--file', 'fixtures/events/workflow_failed.json', '--message-id', messageId];
  const publisher = process.env.WEB_G1_PYTHON;
  const {stdout} = await runCommand(publisher || 'uv',
    publisher ? publisherArgs : ['run', '--project', 'backend', 'python', ...publisherArgs],
    {cwd: root, env: {...process.env, PYTHONPATH: resolve(root, 'backend')}, timeout});
  assert.match(stdout, /RabbitMQ confirmed 1 on events\.workflow/);

  await settlementCard.waitFor();
  assert.equal(await settlementCard.count(), 1);
  assert.equal(settlements.navigations(), settledNavigationCount, 'Card must arrive without navigation');
  const alertId = Number((await settlementCard.getAttribute('href')).split('/').at(-1));
  const created = await until('settlements alert.created', () => settlements.messages.find(
    item => item.type === 'alert.created' && item.team === 'settlements' && item.data.id === alertId));
  assert.equal(created.data.team_id, 'settlements');
  await until('rendered alert in scoped list response', () => settlements.lists.slice(settlementListOffset).some(
    item => item.team === 'settlements' && item.items?.some(alert => alert.id === alertId)));
  assertScopedLists(settlements.lists.slice(settlementListOffset), 'settlements');

  const detail = await json(new URL(`/v1/alerts/${alertId}`, api));
  assert.equal(detail.rule_id, 'settlements-wf-failed');
  assert.equal(detail.occurrence_count, 1);
  assert.equal(detail.occurrences.length, 1);
  const event = await json(new URL(`/v1/events/${detail.occurrences[0].event_id}`, api));
  assert.equal(event.source_event_id, messageId);
  assert.equal(event.transport, 'rabbitmq');
  assert.equal(event.status, 'processed');
  assert.equal(event.fields['$error_code'], 'TIMEOUT');

  const notices = await until('delivered SMS and phone', async () => {
    const {items} = await json(new URL(`/v1/alerts/${alertId}/notifications`, api));
    const external = items.filter(item => item.channel !== 'ui');
    return external.length >= 2 && external.every(item => item.state === 'delivered') && external;
  });
  assert.deepEqual(notices.map(item => `${item.recipient}/${item.channel}`).sort(), ['sam/phone', 'sam/sms']);
  const receipts = (await json(receiptsUrl)).items.filter(item => !priorReceipts.has(item.idempotency_key));
  assert.deepEqual(receipts.map(item => item.idempotency_key).sort(), notices.map(item => item.id).sort());
  assert.deepEqual(receipts.map(item => `${item.to}/${item.channel}`).sort(), [`${samPhone}/phone`, `${samPhone}/sms`]);

  const phone = await context.newPage();
  await phone.goto(telecom.href);
  await until('Sam’s phone option', () => phone.locator('#number option').filter({hasText: samPhone}).count());
  await phone.locator('#number').selectOption(samPhone);
  const phoneCards = phone.locator('#feed article').filter({hasText: title});
  await until('two visible TIMEOUT notices', async () => await phoneCards.count() === 2);
  assert.equal(await phoneCards.getByText('SMS', {exact: true}).count(), 1);
  assert.equal(await phoneCards.getByText('Incoming call', {exact: true}).count(), 1);

  assert.equal(await pricingCard.count(), 0);
  const beforeReload = pricing.lists.length;
  await pricing.page.reload();
  await until('pricing list response after reload', () => pricing.lists.slice(beforeReload).some(row => row.items));
  await pricing.page.getByRole('heading', {name: 'Live feed'}).waitFor();
  assert.equal(await pricing.page.getByRole('textbox', {name: 'Filter by team'}).inputValue(), 'pricing');
  assert.equal(await pricingCard.count(), 0);
  const pricingLists = pricing.lists.slice(pricingListOffset);
  const pricingRequests = pricing.requests.slice(pricingRequestOffset);
  assertScopedLists(pricingLists, 'pricing');
  assert(pricingRequests.every(request => request.team === 'pricing'), 'Pricing reload used another team’s scope');
  const pricingLeaks = pricing.messages.slice(pricingStreamOffset).filter(item => item.data.team_id === 'settlements');
  assert.deepEqual(pricingLeaks, [], 'Pricing stream disclosed an event owned by settlements');
  const screenshots = await Promise.all([
    saveScreenshot(settlements.page, output, 'settlements-timeout.png'),
    saveScreenshot(pricing.page, output, 'pricing-reloaded.png'),
    saveScreenshot(phone, output, 'sam-phone.png'),
  ]);

  const switchRequestOffset = settlements.requests.length;
  const switchListOffset = settlements.lists.length;
  await selectOption(settlements.page, 'Acting as · simulated auth', 'pat');
  await settlementCard.waitFor({state: 'hidden'});
  await until('Pat’s scoped API response after actor switch', () => settlements.lists.slice(switchListOffset).some(
    row => row.team === 'pricing' && row.items));
  const switchedRequests = settlements.requests.slice(switchRequestOffset);
  assert([...settlements.requests, ...pricing.requests].every(request => request.team), 'A browser issued an unscoped feed request');
  assert(switchedRequests.every(request => request.team === 'pricing'), 'Actor switch used another team’s scope');
  assertScopedLists(settlements.lists.slice(switchListOffset), 'pricing');
  assert.equal(await settlements.page.getByRole('textbox', {name: 'Filter by team'}).inputValue(), 'pricing');
  assert.deepEqual([...settlements.errors, ...pricing.errors], [], 'Uncaught browser errors');

  return {
    status: 'PASS',
    publisher: stdout.trim(),
    event: {id: event.id, source_event_id: event.source_event_id, status: event.status, transport: event.transport},
    alert: {id: alertId, title, rule_id: detail.rule_id, owner: detail.team_id, occurrence_count: detail.occurrence_count},
    receipts,
    settlements: {created: created.data, navigations_after_publish: 0, lists: settlements.lists.slice(settlementListOffset, switchListOffset)},
    pricing: {foreign_stream_events: pricingLeaks.length, matching_cards_after_reload: 0,
      lists: pricingLists, requests: pricingRequests, switched_actor_requests: switchedRequests},
    screenshots,
  };
}

async function actedPage({context, frontend}, path, actor) {
  const page = await context.newPage();
  await page.goto(new URL(path, frontend).href);
  await page.getByText('API', {exact: true}).waitFor();
  await selectOption(page, 'Acting as · simulated auth', actor);
  return page;
}

async function clickForResponse(page, button, method, path, status = 200) {
  const [response] = await Promise.all([
    page.waitForResponse(item => item.request().method() === method && new URL(item.url()).pathname === `/v1${path}`),
    page.getByRole('button', {name: button, exact: true}).click(),
  ]);
  assert.equal(response.status(), status, `${method} ${path}: ${await response.text()}`);
  return response.json();
}

async function setRuleYaml(page, rule) {
  await page.getByRole('button', {name: 'YAML', exact: true}).click();
  await page.getByRole('textbox', {name: 'Rule YAML', exact: true}).fill(JSON.stringify(rule, null, 2));
  await page.getByRole('button', {name: 'Apply YAML to builder', exact: true}).click();
}

async function createRule(page, rule) {
  await page.getByRole('button', {name: 'Create using rule editor', exact: true}).click();
  await setRuleYaml(page, rule);
  return clickForResponse(page, 'Save rule', 'POST', '/rules', 201);
}

async function postEvent(api, id, fields, extraHeaders) {
  const result = await json(new URL('/v1/events/generic-webhook', api), {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Api-Key': 'demo-generic-key', 'Idempotency-Key': id, ...extraHeaders},
    body: JSON.stringify(fields),
  });
  assert.equal(result.accepted, 1);
  assert.deepEqual(result.rejected, []);
  assert.equal(result.event_ids.length, 1);
  return result.event_ids[0];
}

async function waitForDecision(api, eventId, ruleId, version, expectedRecipients = []) {
  const result = await until(`event ${eventId} processed`, async () => {
    const explain = await json(new URL(`/v1/events/${eventId}/explain`, api));
    return explain.status === 'processed' && explain;
  });
  const decision = result.decisions.find(item => item.rule_id === ruleId);
  assert(decision, `Event ${eventId} did not record a decision for ${ruleId}`);
  if (version !== null) assert.equal(decision.rule_version, version);
  if (!expectedRecipients.length) {
    assert.equal(decision.outcome, 'NO_MATCH');
    assert.deepEqual(result.notifications, []);
    return {event_id: eventId, rule_version: decision.rule_version, outcome: decision.outcome};
  }
  assert(decision.alert_id, 'Matching event has no alert');
  const notices = await until(`alert ${decision.alert_id} delivered`, async () => {
    const {items} = await json(new URL(`/v1/alerts/${decision.alert_id}/notifications`, api));
    return items.length >= expectedRecipients.length && items.every(item => item.state === 'delivered') && items;
  });
  assert.deepEqual(notices.map(item => `${item.recipient}/${item.channel}`).sort(), [...expectedRecipients].sort());
  return {event_id: eventId, alert_id: decision.alert_id, rule_version: decision.rule_version,
    notifications: notices.map(({id, recipient, channel, state}) => ({id, recipient, channel, state}))};
}

async function ruleHotReload(env) {
  const {api, output, run} = env;
  const page = await actedPage(env, '/rules', 'sam');
  const id = `web-hot-reload-${run.slice(0, 8)}`;
  const rule = {id, owner: 'user:sam', enabled: true,
    match: {all: [{field: 'workflowName', op: 'eq', value: id}, {field: 'probe.phase', op: 'eq', value: 'before'}]},
    route: [{to: 'user:sam', channels: ['sms']}], group: {by: ['workflowId'], mode: 'immediate'},
    present: {title: 'Browser routing check {{workflowId}}', color: 'blue'}};
  await page.getByRole('button', {name: 'Create using rule editor', exact: true}).click();
  await setRuleYaml(page, rule);
  await page.getByRole('textbox', {name: 'Sample event · flattened JSON fields'}).fill(JSON.stringify({workflowName: id, 'probe.phase': 'before'}));
  const test = await clickForResponse(page, 'Test against event', 'POST', '/rules/test');
  assert.equal(test.matched, true);
  const saved = await clickForResponse(page, 'Save rule', 'POST', '/rules', 201);
  assert.equal(saved.version, 1);
  assert.equal(saved.origin, 'ui');

  const workflow = JSON.parse(await readFile(resolve(root, 'fixtures/events/workflow_failed.json'), 'utf8'));
  const before = await postEvent(api, `${id}-before`, {...workflow, workflowName: id, workflowId: `${id}-before`, probe: {phase: 'before'}});
  const previous = await waitForDecision(api, before, id, 1, ['sam/sms']);
  const replacement = {...saved, match: {all: [rule.match.all[0], {...rule.match.all[1], value: 'after'}]},
    route: [{to: 'user:lee', channels: ['phone']}]};
  await setRuleYaml(page, replacement);
  const updated = await clickForResponse(page, 'Save rule', 'PUT', `/rules/${id}`);
  const savedAt = new Date().toISOString();
  const after = await postEvent(api, `${id}-after`, {...workflow, workflowName: id, workflowId: `${id}-after`, probe: {phase: 'after'}});
  assert.equal(updated.version, 2);
  const current = await waitForDecision(api, after, id, 2, ['lee/phone']);
  const stale = await postEvent(api, `${id}-stale`, {...workflow, workflowName: id, workflowId: `${id}-stale`, probe: {phase: 'before'}});
  const ignored = await waitForDecision(api, stale, id, 2);
  await page.getByText('v2', {exact: true}).waitFor();
  const screenshot = await saveScreenshot(page, output, 'rule-version-2.png');
  return {status: 'PASS', id, saved_at: savedAt, before: previous, after: current, old_match_after_save: ignored, screenshot};
}

async function preferencesHotReload(env) {
  const {api, output, run} = env;
  const page = await actedPage(env, '/people', 'anna');
  const first = page.locator('.preference').first();
  await first.getByRole('textbox', {name: /Time window/}).waitFor();
  const actor = {'X-User': 'anna'};
  const preferenceEndpoint = new URL('/v1/users/anna/preferences', api);
  const initial = await json(preferenceEndpoint, {headers: actor});
  assert.equal(initial[0].when, '08:00-20:00');
  assert.deepEqual(initial[0].channels, ['sms']);
  const workflow = JSON.parse(await readFile(resolve(root, 'fixtures/events/workflow_failed.json'), 'utf8'));
  const event = {...workflow, workflowName: 'payments-api', severity: 'CRITICAL'};
  const match = 'anna-payments-critical';
  const clock = {'X-Demo-Clock': '2026-10-23T09:00:00+02:00'};
  const beforeId = await postEvent(api, `web-prefs-${run}-before`, {...event, step: `browser-before-${run}`}, clock);
  const before = await waitForDecision(api, beforeId, match, null, ['anna/sms']);

  const channels = first.getByRole('textbox', {name: 'Channels', exact: true});
  await channels.press('Backspace');
  await channels.click();
  await page.getByRole('option', {name: 'phone', exact: true}).click();
  const saved = await clickForResponse(page, 'Save preferences', 'PUT', '/users/anna/preferences');
  const afterId = await postEvent(api, `web-prefs-${run}-after`, {...event, step: `browser-after-${run}`}, clock);
  const after = await waitForDecision(api, afterId, match, null, ['anna/phone']);
  assert.deepEqual(saved[0].channels, ['phone']);
  await page.reload();
  await page.getByRole('heading', {name: 'My notification preferences'}).waitFor();
  await first.getByText('phone', {exact: true}).waitFor();
  const screenshot = await saveScreenshot(page, output, 'anna-preferences-reloaded.png');
  return {status: 'PASS', actor: 'anna', browser_reload_retained: saved[0], before, after, screenshot};
}

async function unknownFormat(env) {
  const {api, output, run} = env;
  const page = await actedPage(env, '/rules', 'ops');
  const id = `browser-schema-${run.slice(0, 8)}`;
  const format = `${id}.v1`;
  await createRule(page, {id, owner: 'user:ops', enabled: true,
    match: {all: [{field: '$schema', op: 'eq', value: format}, {field: 'signal.outcome', op: 'eq', value: 'notify'}]},
    group: {by: ['signal.id'], mode: 'immediate'}, route: [{to: 'user:ops', channels: ['ui']}],
    present: {title: 'New browser sample {{signal.id}}', color: 'blue'}});
  const knownShape = (key, outcome) => ({signature: format, signal: {id: key, outcome}});
  const raw = knownShape('initial', 'notify');
  const initialId = await postEvent(api, `${id}-initial`, raw);
  assert.equal((await json(new URL(`/v1/events/${initialId}`, api))).status, 'needs_review');

  await page.getByRole('link', {name: 'Onboarding', exact: true}).click();
  const suggestion = await clickForResponse(page, 'Suggest mapping', 'POST', '/schemas/suggest');
  assert(['heuristic', 'llm'].includes(suggestion.source));
  const mapping = {id, name: format, detect: {field: 'signature', op: 'eq', value: format}, severity_default: 'warning', aliases: {error_code: 'signal.outcome'}};
  await page.getByRole('textbox', {name: 'Schema mapping YAML', exact: true}).fill(JSON.stringify(mapping, null, 2));
  await page.getByRole('textbox', {name: 'Should notify · raw JSON', exact: true}).fill(JSON.stringify(raw));
  await page.getByRole('textbox', {name: 'Should stay quiet · raw JSON', exact: true}).fill(JSON.stringify(knownShape('quiet', 'quiet')));
  await page.getByRole('textbox', {name: 'Expected recipients (optional)', exact: true}).fill('ops');
  await page.getByRole('textbox', {name: 'Expected channels (optional)', exact: true}).click();
  await page.getByRole('option', {name: 'ui', exact: true}).click();
  const verified = await clickForResponse(page, 'Verify examples', 'POST', '/schemas/verify');
  assert.equal(verified.all_pass, true);
  assert.deepEqual(verified.samples.map(sample => sample.actual), [{recipients: ['ops'], channels: ['ui']}, {recipients: [], channels: []}]);
  assert.equal((await json(new URL(`/v1/events/${initialId}`, api))).status, 'needs_review');

  const published = await clickForResponse(page, 'Confirm mapping', 'POST', '/schemas');
  const nextId = await postEvent(api, `${id}-new`, knownShape('next', 'notify'));
  const quietId = await postEvent(api, `${id}-quiet`, knownShape('quiet', 'quiet'));
  assert.equal(published.reprocessed, 1);
  assert.equal(published.id, id);
  const [reprocessed, positive, quiet] = await Promise.all([
    waitForDecision(api, initialId, id, 1, ['ops/ui']),
    waitForDecision(api, nextId, id, 1, ['ops/ui']),
    waitForDecision(api, quietId, id, 1),
  ]);
  await page.getByText('Mapping confirmed', {exact: true}).waitFor();
  assert.equal(await page.getByRole('region', {name: 'Verification results'}).getByText('PASS', {exact: true}).count(), 2);
  const screenshot = await saveScreenshot(page, output, 'schema-verify-and-publish.png');
  return {status: 'PASS', suggestion_source: suggestion.source, verification: verified, published, reprocessed, positive, quiet, screenshot};
}

async function assistsAndProposal(env, rule) {
  const {api, output, run} = env;
  const page = await actedPage(env, '/rules', 'sam');
  await page.getByRole('textbox', {name: 'Plain-English request'}).fill('Notify Sam via SMS when severity is critical and service is payments-api');
  const ruleDraft = await clickForResponse(page, 'Generate draft & open editor', 'POST', '/rules/draft');
  assert(['heuristic', 'llm'].includes(ruleDraft.source));
  assert(ruleDraft.draft.match);
  await page.getByRole('heading', {name: 'Rule editor'}).waitFor();
  await page.getByText(`Draft source · ${ruleDraft.source}`, {exact: true}).waitFor();

  await page.getByRole('link', {name: 'Runbooks', exact: true}).click();
  const bookId = `web-runbook-${run.slice(0, 8)}`;
  await page.getByRole('textbox', {name: 'Runbook ID', exact: true}).fill(bookId);
  await page.getByRole('textbox', {name: 'Rule IDs (optional)', exact: true}).fill(rule.id);
  await page.getByRole('textbox', {name: 'Runbook content', exact: true}).fill('1. Check the incident timeline.\n2. Restart the affected service after a lead approves.\n3. Record the result.');
  const structured = await clickForResponse(page, 'Structure with AI', 'POST', '/runbooks/structure');
  assert(['heuristic', 'llm'].includes(structured.source));
  assert(structured.draft.steps.some(step => step.approval === 'required'), 'Expected an approval gate on a restart step');
  await page.getByRole('button', {name: 'Use draft in editor', exact: true}).click();
  const saved = await clickForResponse(page, 'Save runbook', 'POST', '/runbooks', 201);
  const stored = (await json(new URL('/v1/runbooks', api))).items.find(book => book.id === bookId);
  assert.equal(stored.content, structured.draft.content);
  assert.equal(saved.format, 'yaml-steps');
  const runbookScreenshot = await saveScreenshot(page, output, 'runbook-saved.png');

  await page.goto(new URL(`/a/${rule.after.alert_id}`, env.frontend).href);
  await page.getByRole('button', {name: 'Propose fix', exact: true}).click();
  const rationale = `Pause the synthetic browser rule ${rule.id}`;
  await page.getByRole('textbox', {name: 'What should change?', exact: true}).fill(rationale);
  const proposal = await clickForResponse(page, 'Submit proposal', 'POST', '/proposals', 201);
  assert.equal(proposal.proposed_by, 'sam');
  assert.equal(proposal.base_version, 2);
  await page.goto(new URL('/proposals', env.frontend).href);
  const row = page.locator('.step').filter({hasText: rationale});
  await row.getByRole('button', {name: 'Review draft and impact', exact: true}).click();
  const rules = await json(new URL('/v1/rules', api));
  const current = rules.items.find(item => item.id === rule.id);
  await row.getByRole('textbox', {name: 'Proposed rule YAML'}).fill(JSON.stringify({...current, enabled: false}, null, 2));
  const preview = await clickForResponse(page, 'Preview proposal', 'POST', `/rules/${rule.id}/preview`);
  assert(preview.lost_all_notifications.includes(rule.after.event_id), 'Preview omitted the matching v2 event');
  assert.equal((await json(new URL('/v1/rules', api))).items.find(item => item.id === rule.id).version, 2);
  await row.getByRole('region', {name: 'Rule impact'}).waitFor();
  const previewScreenshot = await saveScreenshot(page, output, 'proposal-impact.png');
  const approved = await clickForResponse(page, 'Approve and save rule', 'POST', `/proposals/${proposal.id}/approve`);
  assert.equal(approved.saved_rule.version, 3);
  assert.equal(approved.saved_rule.enabled, false);
  return {status: 'PASS', rule_draft_source: ruleDraft.source, runbook: {id: saved.id, version: saved.version,
    source: structured.source, required_approval_steps: structured.draft.steps.filter(step => step.approval === 'required').length},
  proposal: {id: proposal.id, prior_version: proposal.base_version, published_version: approved.saved_rule.version, preview},
  screenshots: [runbookScreenshot, previewScreenshot]};
}

async function main() {
  const args = process.argv.slice(2);
  assert(args.length <= 1 && args.every(arg => ['--g1-only', '--workflows-only'].includes(arg)),
    'Usage: node tools/browser_g1.mjs [--g1-only | --workflows-only]');
  const frontend = localUrl('WEB_G1_FRONTEND_URL', ['http:']);
  const api = localUrl('ALERTING_API_URL', ['http:']);
  const telecom = localUrl('FAKE_TELECOM_URL', ['http:']);
  localUrl('RABBITMQ_URL', ['amqp:']);
  const output = resolve(process.env.WEB_G1_ARTIFACTS || resolve(root, 'loadtest/results/tmp/web-g1'));
  await mkdir(output, {recursive: true});
  const run = randomUUID();
  const evidence = {run, started_at: new Date().toISOString(), status: 'FAIL', checks: {}};
  let browser;
  try {
    browser = await chromium.launch({channel: process.env.WEB_G1_BROWSER_CHANNEL || undefined});
    const {stdout} = await runCommand('git', ['rev-parse', 'HEAD'], {cwd: root});
    const {stdout: diff} = await runCommand('git', ['status', '--short', '--untracked-files=no'], {cwd: root});
    evidence.environment = {git_head: stdout.trim(), tracked_worktree_changes: diff.trim().split('\n').filter(Boolean),
      chromium: browser.version(), frontend: frontend.origin, api: api.origin, telecom: telecom.origin};
    const context = await browser.newContext({viewport: {width: 1440, height: 900}});
    const env = {context, frontend, api, telecom, output, run};
    async function check(name, runCase) {
      console.log(`Running ${name}`);
      evidence.checks[name] = {status: 'FAIL'};
      try {
        evidence.checks[name] = await runCase();
      } catch (error) {
        evidence.checks[name].error = error.message;
        throw error;
      }
    }
    if (args[0] !== '--workflows-only') await check('settlements', () => settlementG1(env));
    if (args[0] !== '--g1-only') {
      await check('rule_hot_reload', () => ruleHotReload(env));
      await check('preferences_hot_reload', () => preferencesHotReload(env));
      await check('unknown_format', () => unknownFormat(env));
      await check('assists_and_proposal', () => assistsAndProposal(env, evidence.checks.rule_hot_reload));
    }
    evidence.status = 'PASS';
  } catch (error) {
    evidence.error = error.message;
    console.error(error);
    process.exitCode = 1;
  } finally {
    await browser?.close();
    evidence.finished_at = new Date().toISOString();
    const report = resolve(output, 'browser-g1.json');
    await writeFile(report, `${JSON.stringify(evidence, null, 2)}\n`);
    console.log(`${evidence.status} ${report}`);
  }
}

await main();
