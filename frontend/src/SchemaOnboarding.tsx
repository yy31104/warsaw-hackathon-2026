import {useState} from 'react';
import {useNavigate} from 'react-router-dom';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {Badge, Button, MultiSelect, Select, Textarea, TextInput} from '@mantine/core';
import YAML from 'yaml';
import {Data, ErrorBox, PageTitle, Panel, useApp} from './App';
import {request} from './api';

type Fields = Record<string, unknown>;
type Issue = {fingerprint: string; source_id: string; sample_event_id: number; sample_fields: Fields; event_count: number};
type EventSample = {raw: Fields; transport: string};
type Suggestion = {draft: Fields; source: string; rationale?: Record<string, string>};
type SchemaPreview = {matches: boolean; aliases: Fields; missing_paths: string[]; base_version: number};
type Published = {id: string; version: number; reprocessed: number};
type Verification = {
  all_pass: boolean;
  samples: {
    actual: {recipients: string[]; channels: string[]};
    passed: boolean;
    deciding_fields: string[];
    unit_warnings: string[];
  }[];
};

export function SchemaOnboarding() {
  const [selected, setSelected] = useState<Issue | null>(null);
  const data = useQuery({queryKey: ['schema-issues'], queryFn: () => request<{items: Issue[]}>('/schema-issues')});
  const issues = data.data?.items ?? [];
  const issue = selected ?? issues[0];
  return <>
    <PageTitle title="Format onboarding" description="Inspect an event, verify its mapping with examples, then publish the schema."/>
    <ErrorBox error={data.error}/>
    {issues.some(item => item.fingerprint !== issue?.fingerprint) && <Select label="Review issue" value={issue?.fingerprint ?? null}
      data={issues.map(item => ({value: item.fingerprint, label: `${item.source_id} · ${item.event_count} events · ${item.fingerprint}`}))}
      onChange={value => setSelected(issues.find(item => item.fingerprint === value) ?? null)}/>}
    {issue ? <ReviewIssue key={issue.fingerprint} issue={issue} retain={() => setSelected(issue)}/>
      : !data.error && <p>{data.isPending ? 'Loading formats…' : 'No unknown formats need review.'}</p>}
  </>;
}

function parseObject(input: string, format: 'JSON' | 'YAML'): Fields {
  const value = format === 'JSON' ? JSON.parse(input) : YAML.parse(input);
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`Enter a ${format} object.`);
  return value;
}

function ReviewIssue({issue, retain}: {issue: Issue; retain: () => void}) {
  const {actor, directory, notice} = useApp();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [suggestion, setSuggestion] = useState<Suggestion | null>(null);
  const [mapping, setMapping] = useState('');
  const [positive, setPositive] = useState('');
  const [quiet, setQuiet] = useState('{}');
  const [expectedRecipients, setExpectedRecipients] = useState('');
  const [expectedChannels, setExpectedChannels] = useState<string[]>([]);
  const [verification, setVerification] = useState<Verification | null>(null);
  const [preview, setPreview] = useState<SchemaPreview | null>(null);
  const [published, setPublished] = useState<Published | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const fingerprint = encodeURIComponent(issue.fingerprint);
  const team = directory.teams.find(item => item.id === directory.fallback_team);
  const mayPublish = team?.members.includes(actor) || team?.approvers.includes(actor);
  const sample = useQuery({queryKey: ['event-sample', issue.sample_event_id],
    queryFn: () => request<EventSample>(`/events/${issue.sample_event_id}`)});

  async function perform(task: () => Promise<void>) {
    setBusy(true);
    setError('');
    try { await task(); }
    catch (error) { setError(String(error)); }
    finally { setBusy(false); }
  }

  async function suggest() {
    retain();
    const result = await request<Suggestion>('/schemas/suggest', actor, {fingerprint: issue.fingerprint});
    setSuggestion(result);
    setMapping(YAML.stringify(result.draft));
    setPositive(JSON.stringify(sample.data?.raw ?? {}, null, 2));
    setPreview(null);
    setVerification(null);
  }

  async function verify() {
    setVerification(null);
    const recipients = expectedRecipients.split(',').map(value => value.trim()).filter(Boolean);
    const expected = recipients.length || expectedChannels.length ? {
      ...(recipients.length ? {recipients} : {}),
      ...(expectedChannels.length ? {channels: expectedChannels} : {}),
    } : 'notify';
    const source = {source: issue.source_id, transport: sample.data?.transport ?? 'http'};
    setVerification(await request<Verification>('/schemas/verify', actor, {
      schema: parseObject(mapping, 'YAML'),
      samples: [
        {event: parseObject(positive, 'JSON'), expect: expected, note: 'Should notify', ...source},
        {event: parseObject(quiet, 'JSON'), expect: 'quiet', note: 'Should stay quiet', ...source},
      ],
    }));
  }

  async function confirm() {
    const draft = parseObject(mapping, 'YAML');
    const projected = await request<SchemaPreview>(`/schemas/preview?fingerprint=${fingerprint}`, actor, draft);
    setPreview(projected);
    if (!projected.matches) throw new Error('Adjust the draft detection so it selects the reviewed event.');
    const result = await request<Published>(`/schemas?fingerprint=${fingerprint}&base_version=${projected.base_version}`, actor, draft);
    setPublished(result);
    void client.invalidateQueries({queryKey: ['schema-issues']});
    notice(`Schema ${result.id} v${result.version} published`);
  }

  return <>
    <ErrorBox error={error || sample.error}/>
    <Panel title="Inspect unknown format">
      <Badge color={published ? 'teal' : 'gray'}>{published ? 'Mapping confirmed' : 'Needs review'}</Badge>
      <p>{issue.source_id} · {issue.event_count} events · Sample #{issue.sample_event_id}</p>
      <table><thead><tr><th>Field</th><th>Sample</th></tr></thead><tbody>
        {Object.entries(issue.sample_fields).map(([field, value]) => <tr key={field}><td>{field}</td><td>{String(value)}</td></tr>)}
      </tbody></table>
      {!published && <Button disabled={!sample.data} loading={busy && !suggestion} onClick={() => void perform(suggest)}>Suggest mapping</Button>}
    </Panel>
    {suggestion && <Panel title="Review mapping">
      <Badge>Draft source · {suggestion.source}</Badge>
      {Object.values(suggestion.rationale ?? {}).map(item => <p key={item}>{item}</p>)}
      <Textarea label="Schema mapping YAML" autosize minRows={9} maxRows={24} value={mapping}
        readOnly={busy || !!published} onChange={event => {
          setMapping(event.target.value);
          setVerification(null);
          setPreview(null);
        }}/>
      <h3>Verify examples</h3>
      <Textarea label="Should notify · raw JSON" autosize minRows={5} value={positive}
        onChange={event => {setPositive(event.target.value); setVerification(null);}}/>
      <Textarea label="Should stay quiet · raw JSON" autosize minRows={5} value={quiet}
        onChange={event => {setQuiet(event.target.value); setVerification(null);}}/>
      <div className="form-grid">
        <TextInput label="Expected recipients (optional)" placeholder="sam, lee" value={expectedRecipients}
          onChange={event => {setExpectedRecipients(event.target.value); setVerification(null);}}/>
        <MultiSelect label="Expected channels (optional)" searchable data={['email', 'sms', 'phone', 'ui']} value={expectedChannels}
          onChange={value => {setExpectedChannels(value); setVerification(null);}}/>
      </div>
      <div className="actions">
        <Button variant="default" disabled={busy} onClick={() => void perform(verify)}>Verify examples</Button>
        {!published && <Button disabled={busy || !mayPublish} onClick={() => void perform(confirm)}>Confirm mapping</Button>}
      </div>
      {!published && !mayPublish && <p>Schema publication requires a member of {directory.fallback_team ?? 'the fallback team'}.</p>}
      {verification && <section aria-label="Verification results">
        {verification.samples.map((row, index) => <div className="step" key={index}>
          <strong>{index === 0 ? 'Should notify' : 'Should stay quiet'}</strong> <Badge color={row.passed ? 'teal' : 'red'}>{row.passed ? 'PASS' : 'FAIL'}</Badge>
          <p>Recipients {row.actual.recipients.join(', ') || 'none'} · Channels {row.actual.channels.join(', ') || 'none'}</p>
          <p>Deciding fields {row.deciding_fields.join(', ') || 'none'}</p>
          {row.unit_warnings.map(warning => <p className="validation-danger" key={warning}>{warning}</p>)}
        </div>)}
      </section>}
      {preview && <details><summary>Reviewed event fields</summary><Data value={preview}/></details>}
    </Panel>}
    {published && <Panel title="Route new events">
      <p>{published.reprocessed} events requeued for processing.</p>
      <Button onClick={() => navigate('/rules', {state: {sample: {...issue.sample_fields, ...preview?.aliases}}})}>Create rule from this sample</Button>
    </Panel>}
  </>;
}
