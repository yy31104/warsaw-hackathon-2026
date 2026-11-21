const assert = require('node:assert/strict');
const {existsSync, mkdirSync, readFileSync, writeFileSync} = require('node:fs');
const {join, resolve} = require('node:path');

const run = resolve(process.argv[2]);
const {chromium} = require(join(run, 'main/frontend/node_modules/playwright'));
const artifact = join(run, 'innovation-browser');
mkdirSync(artifact, {recursive: true});
const load = name => JSON.parse(readFileSync(join(run, name), 'utf8'));
const {FRONTEND_PORT: port} = load('environment.json');
const origin = `http://127.0.0.1:${port}`;
const walkRelease = process.argv.includes('--release');
const only = process.argv.find(arg => arg.startsWith('--only='))?.split('=')[1];
const {commit} = load('first-alert.json');
const old = only ? load('innovation-browser/result.json') : {commit, cases:{}};
assert.equal(old.commit, commit);
const cases = old.cases;
const save = () => writeFileSync(join(artifact, 'result.json'), JSON.stringify({commit, cases}, null, 2) + '\n');

async function open(page, path, actor) {
  await page.goto(origin + path, {waitUntil: 'domcontentloaded'});
  const intro = page.getByRole('button', {name: 'Open console', exact: true});
  const picker = page.getByLabel('Viewing as', {exact: true});
  await intro.or(picker).first().waitFor({state: 'visible'});
  if (await intro.isVisible()) await intro.click();
  await picker.selectOption(actor);
}

async function observe(browser, id, test) {
  if (only && id !== only) return;
  const context = await browser.newContext({viewport: {width: 1440, height: 900}, reducedMotion: 'reduce'});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', err => errors.push(err.message));
  page.setDefaultTimeout(15000);
  try {
    const observed = await test(page);
    assert.deepEqual(errors, []);
    await page.screenshot({path: join(artifact, id + '.jpg'), type: 'jpeg', quality: 80, animations: 'disabled'});
    cases[id] = {status: 'PASS', ...observed};
  } catch (error) {
    cases[id] = {status: 'FAIL', error: error.message, page_errors: errors};
    await page.screenshot({path: join(artifact, id + '-failure.jpg'), type: 'jpeg', quality: 80}).catch(() => {});
  } finally {
    save();
    console.log(id, cases[id].status, cases[id].error ?? '');
    await context.close();
  }
}

async function responseOnClick(page, button, path, method = 'POST') {
  const [http] = await Promise.all([
    page.waitForResponse(item => item.request().method() === method &&
      (typeof path === 'string' ? new URL(item.url()).pathname === path : path.test(new URL(item.url()).pathname))),
    button.click(),
  ]);
  assert.ok(http.ok(), `${path} returned ${http.status()}`);
  return http.json();
}

async function api(page, path, {actor, data, method} = {}) {
  const response = await page.request.fetch(origin + path, {method: method ?? (data ? 'POST' : 'GET'),
    data, headers: actor ? {'X-User': actor} : {}});
  assert.ok(response.ok(), `${path} returned ${response.status()}`);
  return response.json();
}

async function releaseWalkthrough(browser) {
  await observe(browser, 'proposal-optimizer', async page => {
    const seed = load('unknown-verify.json');
    const text = 'Release check: evaluate policy while keeping SMS';
    const proposal = await api(page, '/v1/proposals', {actor:'sam', data:{alert_id: seed.alert, text}});
    await open(page, '/rules/proposals', 'lee');
    const panel = page.locator('.step').filter({has:page.getByRole('heading', {name:text, exact:true})});
    await panel.getByRole('button', {name:'Review draft and impact', exact:true}).click();
    const report = await responseOnClick(page, panel.getByRole('button', {name:'Find options', exact:true}),
      '/v1/rules/settlements-wf-failed/optimize/preview');
    assert.ok(report.evaluated_count > 0);
    await panel.getByText('Replay scope', {exact:true}).waitFor({state:'visible'});
    await panel.getByText('Replay scope', {exact:true}).scrollIntoViewIfNeeded();
    const proposals = await api(page, '/v1/proposals', {actor:'lee'});
    assert.ok(proposals.items.some(item => item.id === proposal.id && item.status === 'open'));
    return {proposal_id: proposal.id, evaluated_candidates: report.evaluated_count, persisted_status:'open'};
  });
  await observe(browser, 'now-ack', async page => {
    await open(page, '/', 'sam');
    const team = page.getByLabel('Team', {exact:true});
    if (await team.isVisible()) await team.selectOption('settlements');
    const button = page.locator('.cf-hero').getByRole('button', {name:'Acknowledge', exact:true});
    const [response] = await Promise.all([
      page.waitForResponse(item => item.request().method() === 'POST' &&
        /^\/v1\/alerts\/\d+\/ack$/.test(new URL(item.url()).pathname)),
      button.click(),
    ]);
    assert.ok(response.ok());
    const alertId = new URL(response.url()).pathname.split('/')[3];
    const persisted = await api(page, `/v1/alerts/${alertId}`);
    assert.equal(persisted.status, 'acknowledged');
    assert.equal(persisted.acked_by, 'sam');
    return {alert_id:Number(alertId), actor:persisted.acked_by, stored_status:persisted.status};
  });
  await observe(browser, 'on-call-preference', async page => {
    await open(page, '/oncall', 'anna');
    const old = await api(page, '/v1/users/anna/preferences', {actor:'anna'});
    await page.getByRole('button', {name:'Add rule', exact:true}).click();
    const number = old.length + 1;
    await page.getByLabel(`Rule ${number} · severity`, {exact:true}).selectOption('warning');
    await page.getByRole('group', {name:`Rule ${number} · channels`, exact:true})
      .getByRole('button', {name:'Email', exact:true}).click();
    const changed = await responseOnClick(page, page.getByRole('button', {name:'Save preferences', exact:true}),
      '/v1/users/anna/preferences','PUT');
    assert.equal(changed.length, number);
    assert.equal(changed.at(-1).min_severity, 'warning');
    assert.ok(changed.at(-1).channels.includes('email'));
    await page.reload({waitUntil:'domcontentloaded'});
    await page.getByLabel(`Rule ${number} · severity`, {exact:true}).waitFor({state:'visible'});
    assert.equal(await page.getByLabel(`Rule ${number} · severity`, {exact:true}).inputValue(), 'warning');
    return {prior_rows:old.length, saved_rows:number, added_severity:'warning', after_reload:true};
  });
  await observe(browser, 'guided-intro-and-tour', async page => {
    await open(page, '/', 'sam');
    await page.getByRole('button', {name:'Intro', exact:true}).click();
    await page.getByRole('button', {name:'Step 1 · Receive', exact:true}).click();
    await page.getByRole('button', {name:'Next', exact:true}).click();
    await page.getByRole('button', {name:/^Open Rules/}).click();
    await page.waitForURL('**/rules');
    await page.getByRole('button', {name:'Tour', exact:true}).click();
    const tour = page.getByRole('region', {name:'Tour', exact:true});
    await tour.getByText('Start here', {exact:true}).waitFor({state:'visible'});
    await page.waitForURL(origin + '/');
    for (const path of ['/incidents','/sources','/rules','/runbooks','/oncall']) {
      await tour.getByRole('button', {name:/^Next stop/}).click();
      await page.waitForURL(origin + path);
    }
    await tour.getByRole('button', {name:'Done', exact:true}).click();
    await tour.waitFor({state:'hidden'});
    return {intro_manual_navigation:'/rules', tour_stops:6, tour_closed:true};
  });
  await observe(browser, 'themes-and-phone', async page => {
    await open(page, '/oncall', 'anna');
    const root = page.locator('html');
    assert.equal(await root.getAttribute('data-mantine-color-scheme'), 'light');
    await page.getByRole('button', {name:'Toggle theme', exact:true}).click();
    await page.waitForFunction(() => document.documentElement.dataset.mantineColorScheme === 'dark');
    await page.screenshot({path:join(artifact, 'dark-desktop.jpg'), type:'jpeg', quality:80, animations:'disabled'});
    await page.setViewportSize({width:375,height:812});
    const checks = {};
    for (const path of ['/', '/incidents', '/sources', '/rules', '/oncall']) {
      await page.goto(origin + path, {waitUntil:'domcontentloaded'});
      await page.locator('.cf-workspace main').waitFor({state:'visible'});
      const measurements = await page.evaluate(() => ({
        viewport:document.documentElement.clientWidth, document:document.documentElement.scrollWidth,
      }));
      assert.equal(measurements.viewport,375);
      assert.ok(measurements.document <= measurements.viewport, JSON.stringify({path,...measurements}));
      checks[path] = measurements;
    }
    await page.getByRole('button', {name:'Toggle theme', exact:true}).click();
    await page.waitForFunction(() => document.documentElement.dataset.mantineColorScheme === 'light');
    return {themes:['light','dark'], mobile_width:375, mobile_pages:checks};
  });
}

async function main() {
  const browser = await chromium.launch();
  try {
    if (existsSync(join(run, 'stability-live.json'))) {
      await observe(browser, 'decision-stability', async page => {
        const seed = load('stability-live.json');
        await open(page, seed.path, seed.actor);
        await page.getByRole('button', {name: 'Suggest mapping', exact: true}).click();
        await page.getByLabel('Map signal.service', {exact: true}).selectOption('service');
        await page.getByLabel('Map signal.severity', {exact: true}).selectOption('severity');
        const outcome = await responseOnClick(page, page.getByRole('button', {name:'Compare possible interpretations'}),
          '/v1/schemas/stability');
        assert.equal(outcome.status, 'VARIANT');
        assert.equal(outcome.critical_possible, true);
        const comparison = page.getByText('Different interpretations change the decision.', {exact: false});
        await comparison.waitFor({state: 'visible'});
        const warning = page.getByText('Critical possible', {exact: true});
        await warning.waitFor({state: 'visible'});
        const [question] = outcome.ordered_questions;
        assert.equal(question.label, 'P1');
        const quiet = question.candidates.findIndex(candidate => candidate.value === 'warning');
        assert.ok(quiet >= 0);
        const choices = page.locator('.cf-card').filter({
          has:page.getByRole('heading', {name:'Decision stability', exact:true}),
        }).locator('select');
        await choices.selectOption(String(quiet));
        await page.getByText('The answers determine the decision for these samples.', {exact:false}).waitFor({state:'visible'});
        await warning.waitFor({state:'visible'});
        await warning.scrollIntoViewIfNeeded();
        const stillWaiting = await api(page, `/v1/events/${seed.event_id}`);
        assert.equal(stillWaiting.status, 'needs_review');
        return {event_id: seed.event_id, status_from_api: outcome.status, critical_possible: outcome.critical_possible,
          evaluated: outcome.evaluated_combinations, total: outcome.total_combinations,
          quiet_branch_selected:true, critical_warning_remained:true, event_status:stillWaiting.status};
      });
    }
    if (existsSync(join(run, 'optimizer-live.json'))) {
      await observe(browser, 'policy-optimizer', async page => {
        const seed = load('optimizer-live.json');
        await open(page, seed.path, seed.actor);
        await page.getByRole('button', {name: new RegExp(seed.rule_id)}).click();
        const report = await responseOnClick(page, page.getByRole('button', {name:'Find options', exact: true}),
          `/v1/rules/${seed.rule_id}/optimize/preview`);
        const [best] = report.options;
        assert.equal(report.baseline.night_interruptions, 18);
        assert.equal(best.metrics.night_interruptions, 4);
        assert.equal(best.protected_violations, 0);
        const metric = page.getByText('Nighttime planned interruptions', {exact: true}).first();
        await metric.waitFor({state: 'visible'});
        const counterexample = page.getByText(/Planned SMS.*would be missing/i);
        await counterexample.waitFor({state: 'visible'});
        await metric.scrollIntoViewIfNeeded();
        return {rule_id: seed.rule_id, baseline_night: 18, candidate_night: 4, protected_violations: best.protected_violations,
          protected_cases: best.protected_cases, synthetic_seed_events: seed.seed_count};
      });
    }
    if (existsSync(join(run, 'unknown-browser-seed.json'))) {
      await observe(browser, 'unknown-outcome', async page => {
        const seed = load('unknown-browser-seed.json');
        await open(page, seed.incident_path, 'lee');
        const button = page.getByRole('button', {name:'Check receipt', exact: true});
        await button.waitFor({state: 'visible'});
        const result = await responseOnClick(page, button, `/v1/action-runs/${seed.run_id}/reconcile`);
        assert.equal(result.id, seed.run_id);
        assert.equal(result.state, 'succeeded');
        await button.waitFor({state: 'hidden'});
        await page.locator('#runbook').scrollIntoViewIfNeeded();
        return {alert_id: seed.alert, run_id: seed.run_id, status_from_api: result.state};
      });
    }
    if (walkRelease) await releaseWalkthrough(browser);
  } finally {
    await browser.close();
  }
  assert.ok(Object.values(cases).every(row => row.status === 'PASS'), 'One or more innovation browser checks failed.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
