import {useState} from 'react';
import {useLocation} from 'react-router-dom';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {useApp} from '../App';
import {request} from '../api';
import {StabilityPanel} from '../StabilityPanel';
import {requestProblems, type Problem} from './data';
import {Card, channelName, Chip, Failure, humanize, LegacyLink, personName, Problems} from './ui';

type Fields = Record<string, unknown>;
type Issue = {fingerprint: string; source_id: string; sample_event_id: number; event_count: number};
type Draft = Fields & {
  id: string; name: string; aliases?: Record<string, string>; event_id?: string;
  severity_map?: Record<string, string>; time_format?: string;
  transforms?: Record<string, {scale: number | string; unit?: string}>;
};
type Suggestion = {draft: Draft; source: string; model?: string; rationale?: Record<string, string>};
type VerifySample = {
  passed: boolean; actual: {recipients: string[]; channels: string[]}; deciding_fields: string[];
  unit_warnings?: string[]; problems?: Problem[];
};
type Verification = {all_pass: boolean; samples: VerifySample[]; problems?: Problem[]};
type Projection = {matches: boolean; base_version: number};
type Published = {id: string; version: number; reprocessed: number};
const roles = ['service', 'severity', 'environment', 'error_code', 'message', 'time', 'event_id'];
const severities = ['critical', 'error', 'warning', 'info', 'debug', 'unknown'];

function mappedSeverity(draft: Draft, value: string) {
  return draft.severity_map?.[value] ?? Object.entries(draft.severity_map ?? {})
    .find(([label]) => label.toLowerCase() === value.toLowerCase())?.[1];
}

function parseSample(value: string) {
  const parsed: unknown = JSON.parse(value);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Enter a JSON object.');
  return parsed;
}

function rawFields(value: unknown, prefix = ''): [string, unknown][] {
  return value && typeof value === 'object'
    ? Object.entries(value).flatMap(([name, child]) => rawFields(child, prefix ? prefix + '.' + name : name))
    : [[prefix, value]];
}

function rawLines(value: unknown, path = '', depth = 0, label = '', suffix = ''): {path: string; text: string}[] {
  const indent = '  '.repeat(depth);
  if (!value || typeof value !== 'object' || !Object.keys(value).length) {
    return [{path, text: indent + label + JSON.stringify(value) + suffix}];
  }
  const array = Array.isArray(value);
  const entries = Object.entries(value);
  return [
    {path, text: indent + label + (array ? '[' : '{')},
    ...entries.flatMap(([key, child], index) => rawLines(child, path ? path + '.' + key : key, depth + 1,
      array ? '' : JSON.stringify(key) + ': ', index === entries.length - 1 ? '' : ',')),
    {path, text: indent + (array ? ']' : '}') + suffix},
  ];
}

function RawSample({raw, highlight}: {raw: Fields; highlight: Set<string>}) {
  return <pre className="cf-code" tabIndex={0} aria-label="Raw sample"><code>
    {rawLines(raw).map((line, index) => <span key={index}>
      {highlight.has(line.path) ? <mark>{line.text}</mark> : line.text}{'\n'}
    </span>)}
  </code></pre>;
}

function MappingRows({draft, raw, change, flagged}: {
  draft: Draft; raw: Fields; change: (draft: Draft) => void; flagged: Set<string>;
}) {
  function mapField(path: string, role: string) {
    const aliases = {...draft.aliases};
    for (const [name, mapped] of Object.entries(aliases)) if (mapped === path) delete aliases[name];
    if (role && role !== 'event_id') aliases[role] = path;
    const identity = role === 'event_id' ? '{{' + path + '}}'
      : draft.event_id === '{{' + path + '}}' ? undefined : draft.event_id;
    change({...draft, aliases, event_id: identity});
  }

  return <div className="cf-mapping">
    {rawFields(raw).map(([path, value], index) => {
      const aliases = Object.entries(draft.aliases ?? {}).filter(([, mapped]) => mapped === path).map(([alias]) => alias);
      const alias = draft.event_id === '{{' + path + '}}' ? 'event_id' : aliases[0] ?? '';
      const transform = draft.transforms?.[path];
      const numeric = typeof value === 'number' || typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value));
      const severity = String(value);
      return <div className="cf-map-row" data-flagged={flagged.has(path)} key={path + index}>
        <div className="cf-map-path"><code>{path}</code><small>{JSON.stringify(value)}</small></div>
        <span aria-hidden="true">→</span>
        <div className="cf-map-answers">
          <select aria-label={`Map ${path}`} value={alias} onChange={event => mapField(path, event.target.value)}>
            <option value="">Keep field</option>
            {roles.map(role => <option key={role} value={role}>{role === 'event_id' ? 'Event ID' : humanize(role)}</option>)}
          </select>
          {aliases.length > 1 && <small>{aliases.slice(1).map(humanize).join(' · ')}</small>}
          {aliases.includes('severity') && <label className="cf-field">Severity for {severity}
            <select aria-label={`Severity for ${severity}`} value={mappedSeverity(draft, severity) ?? ''} onChange={event => {
              const mapping = {...draft.severity_map};
              if (event.target.value) mapping[severity] = event.target.value;
              else delete mapping[severity];
              change({...draft, severity_map: mapping});
            }}><option value="">Choose severity</option>{severities.map(level => <option key={level} value={level}>{humanize(level)}</option>)}</select>
          </label>}
          {aliases.includes('time') && <label className="cf-field">Time format
            <select value={draft.time_format ?? 'iso'} onChange={event => change({...draft, time_format: event.target.value})}>
              <option value="iso">ISO timestamp</option><option value="epoch_s">Epoch seconds</option><option value="epoch_ms">Epoch milliseconds</option>
            </select>
          </label>}
          {numeric && <details open={Boolean(transform || flagged.has(path))}>
            <summary>{transform ? 'Unit and scale' : 'Set units'}</summary>
            <div className="cf-form-row">
              <label className="cf-field">Unit<input value={transform?.unit ?? ''} aria-label={`Unit for ${path}`}
                onChange={event => {
                  const unit = event.target.value;
                  change({...draft, transforms: {...draft.transforms, [path]: {scale: transform?.scale ?? 1, unit: unit || undefined}}});
                }}/></label>
              <label className="cf-field">Scale<input type="number" step="any" value={transform?.scale ?? ''} aria-label={`Scale for ${path}`}
                onChange={event => {
                  const scale = event.target.value;
                  change({...draft, transforms: {...draft.transforms, [path]: {...transform, scale}}});
                }}/></label>
            </div>
            {transform && <button className="cf-btn" onClick={() => {
              const transforms = {...draft.transforms};
              delete transforms[path];
              change({...draft, transforms});
            }}>Clear units</button>}
          </details>}
        </div>
      </div>;
    })}
  </div>;
}

function SampleResult({row}: {row?: VerifySample}) {
  if (!row) return null;
  return <div className="cf-result" role="status">
    <Chip tone={row.passed ? 'green' : 'red'}>{row.passed ? 'Pass' : 'Fail'}</Chip>
    <strong>{row.actual.recipients.join(', ') || 'Nobody'}</strong>
    {row.actual.channels.length > 0 && <small>{row.actual.channels.join(' + ')}</small>}
    {row.deciding_fields.length > 0 && <div className="cf-chips">{row.deciding_fields.map(field => <Chip key={field}><code>{field}</code></Chip>)}</div>}
    <Problems items={row.problems}/>
    <Problems items={row.unit_warnings?.map(path => ({code: 'unit_unclear', path, message: 'Confirm the unit and scale.'}))}/>
  </div>;
}

function Review({issue, retain}: {issue: Issue; retain: () => void}) {
  const {actor, directory, notice} = useApp();
  const client = useQueryClient();
  const [suggestion, setSuggestion] = useState<Suggestion>();
  const [draft, setDraft] = useState<Draft>();
  const [positive, setPositive] = useState('');
  const [quiet, setQuiet] = useState('');
  const [recipients, setRecipients] = useState('');
  const [channels, setChannels] = useState<string[]>([]);
  const [verification, setVerification] = useState<Verification>();
  const [projection, setProjection] = useState<Projection>();
  const [published, setPublished] = useState<Published>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [highlight, setHighlight] = useState<string[]>([]);
  const sample = useQuery({queryKey: ['event-sample', issue.sample_event_id], queryFn: () =>
    request<{raw: Fields; transport: string}>(`/events/${issue.sample_event_id}`)});
  const fallback = directory.teams.find(team => team.id === directory.fallback_team);
  const mayPublish = fallback && (fallback.members.includes(actor) || fallback.approvers.includes(actor));
  const reviewers = [...new Set([...(fallback?.members ?? []), ...(fallback?.approvers ?? [])])]
    .map(person => personName(person, directory));
  const fingerprint = encodeURIComponent(issue.fingerprint);
  const severityPath = draft?.aliases?.severity;
  const severity = sample.data && severityPath && rawFields(sample.data.raw).find(([path]) => path === severityPath)?.[1];
  const needsSeverity = draft && severity != null && !mappedSeverity(draft, String(severity));
  const ready = verification?.all_pass && projection?.matches && !needsSeverity && !published;

  async function perform(task: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try { await task(); } catch (problem) { setError(problem); } finally { setBusy(false); }
  }

  function invalidateTest() {
    setVerification(undefined);
    setProjection(undefined);
  }

  async function suggest() {
    if (!sample.data) return;
    const result = await request<Suggestion>('/schemas/suggest', actor, {fingerprint: issue.fingerprint});
    retain();
    setSuggestion(result);
    setDraft(result.draft);
    setPositive(JSON.stringify(sample.data.raw, null, 2));
    invalidateTest();
  }

  async function verify() {
    if (!draft || !sample.data) return;
    invalidateTest();
    const expectedRecipients = recipients.split(',').map(value => value.trim()).filter(Boolean);
    const expect = expectedRecipients.length || channels.length ? {
      ...(expectedRecipients.length ? {recipients: expectedRecipients} : {}),
      ...(channels.length ? {channels} : {}),
    } : 'notify';
    const transport = {transport: sample.data.transport, source: issue.source_id};
    const result = await request<Verification>('/schemas/verify', actor, {schema: draft, samples: [
      {event: parseSample(positive), expect, ...transport},
      {event: parseSample(quiet), expect: 'quiet', ...transport},
    ]});
    setVerification(result);
    setHighlight(result.samples.flatMap(item => [...(item.unit_warnings ?? []), ...(item.problems ?? [])
      .flatMap(problem => problem.path ? [problem.path] : [])]));
    if (result.all_pass) {
      const reviewed = await request<Projection>(`/schemas/preview?fingerprint=${fingerprint}`, actor, draft);
      setProjection(reviewed);
      if (!reviewed.matches) throw new Error('Draft does not match the waiting sample.');
    }
  }

  async function publish() {
    if (!draft || !projection || !ready || !mayPublish) return;
    const confirmed = await request<Published>(`/schemas?fingerprint=${fingerprint}&base_version=${projection.base_version}`, actor, draft);
    setPublished(confirmed);
    notice('Schema is live');
    void client.invalidateQueries({queryKey: ['schema-issues']});
  }

  const problems = requestProblems(error);
  const flagged = new Set([...highlight, ...problems.flatMap(problem => problem.path ? [problem.path] : [])]);
  if (severityPath && needsSeverity) flagged.add(severityPath);

  return <div className="cf-stack">
    <ol className="cf-progress" aria-label="Source setup">
      {['Answer', 'Test', 'Go live'].map((step, index) => <li key={step}
        aria-current={index === (ready || published ? 2 : draft ? 1 : 0) ? 'step' : undefined}><span>{index + 1}</span> {step}</li>)}
    </ol>
    {error && !problems.length ? <Failure error={error}/> : <Problems items={problems}/>}
    <Failure error={sample.error}/>
    <div className="cf-grid">
      <Card title={issue.source_id} meta={`${issue.event_count} waiting · live`}>
        {sample.data ? <RawSample raw={sample.data.raw} highlight={flagged}/> : <p>Loading sample…</p>}
        {flagged.size > 0 && <small>Highlighted fields need a person.</small>}
      </Card>
      <Card title="What it means" meta={suggestion
        ? [suggestion.source === 'llm' ? 'AI suggested' : 'Rules suggested', suggestion.model].filter(Boolean).join(' · ')
        : undefined}>
        {!draft && <button className="cf-btn primary" disabled={!sample.data || busy} onClick={() => void perform(suggest)}>Suggest mapping</button>}
        {draft && sample.data && <fieldset className="cf-fieldset" disabled={busy || Boolean(published)}>
          <div className="cf-form-row">
            <label className="cf-field">Schema ID<input value={draft.id} onChange={event => {
              setDraft({...draft, id: event.target.value}); invalidateTest();
            }}/></label>
            <label className="cf-field">Schema name<input value={draft.name} onChange={event => {
              setDraft({...draft, name: event.target.value}); invalidateTest();
            }}/></label>
          </div>
          <MappingRows raw={sample.data.raw} draft={draft} flagged={flagged} change={next => {setDraft(next); invalidateTest();}}/>
          <details className="cf-form-actions">
            <summary>Format detection</summary>
            <pre className="cf-code">{JSON.stringify(draft.detect, null, 2)}</pre>
            <button className="cf-btn" disabled={!Object.keys(sample.data.raw).length} onClick={() => {
              const all = Object.keys(sample.data!.raw).filter(Boolean).map(field => ({field, op: 'exists'}));
              if (!all.length) return;
              setDraft({...draft, detect: {all}});
              invalidateTest();
            }}>Match sample shape</button>
          </details>
        </fieldset>}
      </Card>
    </div>
    {draft && sample.data && !published && <StabilityPanel key={JSON.stringify([draft, positive, quiet, actor])} draft={draft} positive={positive} quiet={quiet} source={issue.source_id} transport={sample.data.transport} actor={actor} suggestion={suggestion}/>}
    {draft && <Card title="Test before going live" meta="Dry run">
      <fieldset className="cf-fieldset cf-grid" disabled={busy || Boolean(published)}>
        <div className="cf-sample-box"><label className="cf-field">Should alert
          <textarea rows={5} aria-label="Should alert" spellCheck={false} value={positive} onChange={event => {
            setPositive(event.target.value); invalidateTest();
          }}/></label>
          <label className="cf-field">Expected recipients<input value={recipients} onChange={event => {
            setRecipients(event.target.value); invalidateTest();
          }}/></label>
          <div className="cf-checkboxes" role="group" aria-label="Expected channels">
            {['sms', 'email', 'phone', 'ui'].map(channel => <label key={channel}><input type="checkbox" checked={channels.includes(channel)}
              onChange={event => {
                setChannels(event.target.checked ? [...channels, channel] : channels.filter(value => value !== channel));
                invalidateTest();
              }}/>{channelName(channel)}</label>)}
          </div>
          <SampleResult row={verification?.samples[0]}/>
        </div>
        <div className="cf-sample-box"><label className="cf-field">Should stay quiet
          <textarea rows={5} aria-label="Should stay quiet" spellCheck={false} value={quiet} onChange={event => {
            setQuiet(event.target.value); invalidateTest();
          }}/></label>
          <SampleResult row={verification?.samples[1]}/>
        </div>
      </fieldset>
      <Problems items={verification?.problems}/>
      <div className="cf-actions cf-form-actions">
        <button className={`cf-btn ${ready || published ? '' : 'primary'}`}
          disabled={busy || !positive.trim() || !quiet.trim() || Boolean(published)} onClick={() => void perform(verify)}>Run test</button>
        <button className={`cf-btn ${ready ? 'primary' : ''}`} disabled={!ready || !mayPublish || busy}
          onClick={() => void perform(publish)}>Go live</button>
        {published && <Chip tone="green">{published.reprocessed} waiting events re-checked</Chip>}
        {!published && needsSeverity && <small>Confirm the source severity.</small>}
        {!mayPublish && reviewers.length > 0 && <small>Publish as {reviewers.join(' / ')}.</small>}
      </div>
    </Card>}
  </div>;
}

export function Sources() {
  const [selected, setSelected] = useState('');
  const [retained, setRetained] = useState<Issue>();
  const location = useLocation();
  const data = useQuery({queryKey: ['schema-issues'], queryFn: () => request<{items: Issue[]}>('/schema-issues')});
  const issues = data.data?.items ?? [];
  const issue = issues.find(item => item.fingerprint === selected) ?? retained
    ?? issues.find(item => item.fingerprint === new URLSearchParams(location.search).get('fingerprint')) ?? issues[0];
  const choices = retained && !issues.some(item => item.fingerprint === retained.fingerprint) ? [retained, ...issues] : issues;
  return <div className="cf-stack">
    <Failure error={data.error}/>
    {issue && <label className="cf-field cf-picker">Review issue<select value={issue.fingerprint} onChange={event => {
      setSelected(event.target.value); setRetained(undefined);
    }}>
      {choices.map(item => <option key={item.fingerprint} value={item.fingerprint}>
        {item.source_id} · {item.event_count} waiting · {item.fingerprint}
      </option>)}
    </select></label>}
    {issue ? <Review key={issue.fingerprint} issue={issue} retain={() => setRetained(issue)}/>
      : data.isSuccess ? <Card title="No formats waiting"><p>No events waiting for review.</p></Card> : <p>Loading sources…</p>}
    <LegacyLink to="/onboarding">Advanced mapping</LegacyLink>
  </div>;
}
