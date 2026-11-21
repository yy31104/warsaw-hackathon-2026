import {useState} from 'react';
import {useLocation} from 'react-router-dom';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {useApp} from '../App';
import {request} from '../api';
import type {ImpactPreviewResult} from '../ImpactPreview';
import {OptimizerPanel} from '../OptimizerPanel';
import {ownsRule} from '../permissions';
import {applyRulePathSuggestion, requestProblems, type EditableRule, type Problem} from './data';
import {Card, channelName, Chip, Failure, LegacyLink, personName, Problems, teamName} from './ui';
import type {Directory} from '../types';

const ownerLabel = (owner: string, directory: Directory) => owner.startsWith('team:')
  ? teamName(owner.slice(5), directory) : owner.startsWith('user:') ? personName(owner.slice(5), directory) : owner;

type Match = Record<string, unknown>;
type DraftRule = EditableRule;
type RuleSuggestion = {draft: DraftRule; source: 'heuristic' | 'llm'; model?: string; warnings?: string[]};
type Check = {problems?: Problem[]; scenarios_checked: unknown[];
  violations: {scenario: unknown; problem: string; requirement?: unknown}[]};

const operators = [
  ['eq', '='], ['neq', '≠'], ['in', 'In'], ['not_in', 'Not in'], ['gt', '>'], ['gte', '≥'],
  ['lt', '<'], ['lte', '≤'], ['glob', 'Matches'], ['regex', 'Regex'], ['contains', 'Contains'],
  ['exists', 'Exists'], ['not_exists', 'Missing'],
];
const channels = ['phone', 'sms', 'email', 'ui'];
const newCondition = () => ({field: '', op: 'eq', value: ''});

function decodeValue(text: string, operator: string): unknown {
  if (operator === 'in' || operator === 'not_in') {
    try { const list: unknown = JSON.parse(text); if (Array.isArray(list)) return list; } catch { /* comma list */ }
    return text.split(',').map(item => decodeValue(item.trim(), 'eq'));
  }
  if (['glob', 'regex', 'contains'].includes(operator)) return text;
  try { return JSON.parse(text); } catch { return text; }
}

function Predicate({node, prefix, change, remove}: {
  node: Match; prefix: string; change: (item: Match) => void; remove?: () => void;
}) {
  const [text, setText] = useState<string>();
  const operator = String(node.op);
  const noValue = operator === 'exists' || operator === 'not_exists';
  return <div className="cf-condition">
    <span className="cf-meta">{prefix}</span>
    <label className="cf-field">Field<input value={String(node.field)} onChange={event => change({...node, field: event.target.value})}/></label>
    <label className="cf-field">Operator<select value={operator} onChange={event => {
      const op = event.target.value;
      setText(undefined);
      change({...node, op, value: op === 'in' || op === 'not_in' ? [node.value ?? ''] : node.value ?? ''});
    }}>{operators.map(([name, label]) => <option value={name} key={name}>{label}</option>)}</select></label>
    <label className="cf-field">Value<input disabled={noValue} type="text"
      value={text ?? (typeof node.value === 'string' ? node.value : JSON.stringify(node.value) ?? '')}
      onBlur={() => setText(undefined)} onChange={event => {
        setText(event.target.value);
        change({...node, value: decodeValue(event.target.value, operator)});
      }}/></label>
    {remove && <button className="cf-btn" aria-label="Remove condition" onClick={remove}>×</button>}
  </div>;
}

function Conditions({node, change, prefix = 'When', remove}: {
  node: Match; change: (item: Match) => void; prefix?: string; remove?: () => void;
}) {
  if (typeof node.field === 'string') return <Predicate node={node} prefix={prefix} change={change} remove={remove}/>;
  if ('not' in node) return <div className="cf-condition-group">
    <Conditions node={node.not as Match} prefix="Not" change={item => change({not: item})} remove={remove}/>
  </div>;
  const mode = Array.isArray(node.any) ? 'any' : 'all';
  const terms = (Array.isArray(node[mode]) ? node[mode] : Object.entries(node).map(([field, value]) => ({field, op: 'eq', value}))) as Match[];
  return <div className="cf-condition-group">
    <div className="cf-actions">
      <label className="cf-field">Match<select value={mode} onChange={event => change({[event.target.value]: terms})}>
        <option value="all">All (AND)</option><option value="any">Any (OR)</option>
      </select></label>
      {remove && <button className="cf-btn" onClick={remove}>Remove group</button>}
    </div>
    {terms.map((item, index) => <Conditions key={index} node={item}
      prefix={index === 0 ? prefix : mode === 'all' ? 'And' : 'Or'}
      change={next => change({[mode]: terms.map((prior, position) => position === index ? next : prior)})}
      remove={terms.length > 1 ? () => change({[mode]: terms.filter((_, position) => position !== index)}) : undefined}/>)}
    <div className="cf-actions">
      <button className="cf-btn" onClick={() => change({[mode]: [...terms, newCondition()]})}>Add condition</button>
      <button className="cf-btn" onClick={() => change({[mode]: [...terms, {any: [newCondition()]}]})}>Add group</button>
    </div>
  </div>;
}

function Impact({result}: {result?: ImpactPreviewResult}) {
  if (!result) return null;
  const count = (rows: Record<string, number>) => Object.values(rows).reduce((sum, item) => sum + item, 0);
  const values = [
    {label: 'Messages', before: count(result.current.notifications), after: count(result.draft.notifications)},
    {label: 'Incidents', before: result.current.alerts, after: result.draft.alerts},
  ];
  return <div className="cf-stack">
    <dl className="cf-comparison">{values.map(item => <div key={item.label}>
      <dt>{item.label}</dt><dd>{item.before} → <strong>{item.after}</strong></dd>
    </div>)}</dl>
    <Chip tone={result.lost_all_notifications.length ? 'red' : 'neutral'}>{result.lost_all_notifications.length} events would notify no one</Chip>
    {!!result.lost_recipients?.length && <small>Lost recipients · {result.lost_recipients.join(', ')}</small>}
    <small>Replay · N={result.events_replayed} events</small>
  </div>;
}

export function ConsoleRules() {
  const {actor, directory, notice} = useApp();
  const location = useLocation();
  const client = useQueryClient();
  const rules = useQuery({queryKey: ['rules'], queryFn: async () => (await request<{items: DraftRule[]}>('/rules')).items});
  const [draft, setDraft] = useState<DraftRule | null>(null);
  const [base, setBase] = useState<DraftRule | null>(null);
  const [text, setText] = useState('');
  const [assistant, setAssistant] = useState<RuleSuggestion>();
  const [check, setCheck] = useState<Check>();
  const [impact, setImpact] = useState<ImpactPreviewResult>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const locked = Boolean(base && (base.origin === 'file' || !ownsRule(base.owner, actor, false, directory)));
  const options = [
    ...directory.teams.flatMap(item => [
      {value: `team:${item.id}`, label: teamName(item.id, directory)},
      ...(item.oncall ? [{value: `oncall:${item.id}`, label: teamName(item.id, directory) + ' on call'}] : []),
    ]),
    ...directory.users.map(person => ({value: `user:${person.id}`, label: personName(person.id, directory)})),
  ];
  const ownerTeam = directory.teams.find(team => draft?.owner === `team:${team.id}`)
    ?? directory.teams.find(team => team.members.some(person => draft?.owner === `user:${person}`));

  function change(rule: DraftRule | null) {
    setDraft(rule);
    setCheck(undefined);
    setImpact(undefined);
  }

  function open(rule: DraftRule | null, original: DraftRule | null = null) {
    change(rule);
    setBase(original);
    setAssistant(undefined);
    setError(null);
  }

  function start() {
    const sample = location.state?.sample as Record<string, unknown> | undefined;
    const first = sample ? Object.entries(sample).find(([field, value]) => !field.startsWith('$')
      && (typeof value === 'string' || typeof value === 'number')) : undefined;
    open({id: '', owner: `user:${actor}`, origin: 'ui', version: 0, enabled: true,
      match: {all: [first ? {field: first[0], op: 'eq', value: first[1]} : newCondition()]},
      route: [{to: `user:${actor}`, channels: ['ui']}]});
  }

  async function perform(task: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try { await task(); } catch (problem) { setError(problem); } finally { setBusy(false); }
  }

  async function publish(rule: DraftRule) {
    const {id} = rule;
    const input = assistant ? {...rule, source: assistant.source} : rule;
    const saved = base
      ? await request<DraftRule>(`/rules/${encodeURIComponent(id)}`, actor, {...input, version: base.version}, 'PUT')
      : await request<DraftRule>('/rules', actor, input);
    open(saved, saved);
    void client.invalidateQueries({queryKey: ['rules']});
    notice(`Published ${saved.id} · v${saved.version}`);
  }

  return <div className="cf-stack">
    <nav className="cf-tabs" aria-label="Rule tools">
      <strong aria-current="page">Rules</strong><LegacyLink to="/rules/proposals">Proposals</LegacyLink>
      <LegacyLink to="/rules/advanced">Advanced editor</LegacyLink>
    </nav>
    <Failure error={rules.error || (requestProblems(error).length === 0 ? error : null)}/>
    <Problems items={requestProblems(error)}/>
    {!draft && <div className="cf-grid cf-grid-wide">
      <Card title="Configured rules" meta={rules.isSuccess && `${rules.data.length} · live`}>
        {(rules.data ?? []).map(item => <button className="cf-rule-row" key={item.id} onClick={() => open(item, item)}>
          <div><strong>{item.id}</strong><small>{ownerLabel(item.owner, directory)} · v{item.version} · {item.origin === 'file' ? 'config file' : 'made here'}</small></div>
          <Chip tone={!item.enabled ? 'amber' : 'neutral'}>{item.enabled ? 'Enabled' : 'Paused'}</Chip>
        </button>)}
        <div className="cf-foot"><button className="cf-btn primary" onClick={start}>New rule</button></div>
      </Card>
      <Card title="Describe a rule">
        <label className="cf-field">Rule in words<textarea rows={4} value={text} placeholder="e.g. Call the settlements on-call by phone when a production settlement workflow fails"
          onChange={event => setText(event.target.value)}/></label>
        <button className="cf-btn" disabled={!text.trim() || busy} onClick={() => void perform(async () => {
          const result = await request<RuleSuggestion>('/rules/draft', actor, {text});
          open({...result.draft, origin: 'ui'});
          setAssistant(result);
        })}>Draft rule</button>
      </Card>
    </div>}
    {draft && <>
      <div className="cf-actions">
        <button className="cf-btn" disabled={busy} onClick={() => open(null)}>← All rules</button>
        {locked && <button className="cf-btn" onClick={() => open({...draft, id: draft.id + '-copy', owner: `user:${actor}`,
          origin: 'ui', version: 0})}>Make a copy</button>}
        {locked && <span className="cf-meta">{base?.origin === 'file' ? 'File managed' : 'Read-only'}</span>}
      </div>
      <div className="cf-grid cf-grid-wide">
        <Card title={base?.id ?? 'Rule editor'} meta={base ? `v${base.version} · ${base.origin}` : assistant
          ? [assistant.source === 'llm' ? 'AI draft' : 'Rules draft', assistant.model].filter(Boolean).join(' · ') : undefined}>
          <fieldset className="cf-fieldset cf-stack" disabled={busy || locked}>
            <div className="cf-form-row">
              <label className="cf-field">Rule ID<input disabled={Boolean(base)} value={draft.id}
                onChange={event => change({...draft, id: event.target.value})}/></label>
              <label className="cf-inline"><input type="checkbox" checked={draft.enabled} onChange={event =>
                change({...draft, enabled: event.target.checked})}/>Enabled</label>
            </div>
            <Conditions node={draft.match} change={match => change({...draft, match})}/>
            <h3>Then notify</h3>
            {draft.route.map((route, index) => <div className="cf-route" key={index}>
              <label className="cf-field">Recipient<select value={route.to} onChange={event => change({...draft,
                route: draft.route.map((row, position) => position === index ? {...row, to: event.target.value} : row)})}>
                {!options.some(item => item.value === route.to) && <option value={route.to}>{route.to}</option>}
                {options.map(item => <option value={item.value} key={item.value}>{item.label}</option>)}
              </select></label>
              <div className="cf-checkboxes" role="group" aria-label={`Channels for route ${index + 1}`}>
                {channels.map(channel => <label key={channel}><input type="checkbox"
                  checked={route.channels !== 'preferences' && route.channels.includes(channel)}
                  onChange={event => {
                    const current = route.channels === 'preferences' ? [] : route.channels;
                    const selected = event.target.checked ? [...current, channel] : current.filter(value => value !== channel);
                    change({...draft, route: draft.route.map((row, position) => position === index
                      ? {...row, channels: selected.length ? selected : 'preferences'} : row)});
                  }}/>{channelName(channel)}</label>)}
                {route.channels === 'preferences' && <Chip>Preferences</Chip>}
              </div>
              {draft.route.length > 1 && <button className="cf-btn" onClick={() => change({...draft,
                route: draft.route.filter((_, position) => position !== index)})}>Remove route</button>}
            </div>)}
            <div><button className="cf-btn" onClick={() => change({...draft,
              route: [...draft.route, {to: `user:${actor}`, channels: 'preferences'}]})}>Add recipient</button></div>
            {ownerTeam?.oncall?.secondary && <label className="cf-field">If no ack
              <input aria-label="Escalate after" value={draft.escalate_after ?? ''} placeholder={ownerTeam.escalate_after ?? 'e.g. 10m'}
                onChange={event => change({...draft, escalate_after: event.target.value || undefined})}/>
              <small>Then {personName(ownerTeam.oncall.secondary, directory)}</small>
            </label>}
            <label className="cf-field">Alert title<input value={draft.present?.title ?? ''}
              onChange={event => change({...draft, present: {...draft.present, title: event.target.value}})}/></label>
          </fieldset>
          <Problems items={check?.problems} suggest={(from, to) => change(applyRulePathSuggestion(draft, from, to))}/>
          {assistant?.warnings?.map(warning => <small key={warning} className="cf-meta">{warning}</small>)}
        </Card>
        <div className="cf-stack">
          <Card title="If you publish">
            <Impact result={impact}/>
            <button className="cf-btn" disabled={!draft.id.trim() || busy} onClick={() => void perform(async () =>
              setImpact(await request<ImpactPreviewResult>(`/rules/${encodeURIComponent(draft.id)}/preview`, actor, {draft, last_n: 500})))}>Preview impact</button>
          </Card>
          <Card title="Safety check" meta={check && `N=${check.scenarios_checked.length} scenarios`}>
            {check?.violations.map((item, index) => <div className="cf-violation" key={index}>
              <strong>{item.problem}</strong><small>{typeof item.scenario === 'string' ? item.scenario : JSON.stringify(item.scenario)}</small>
            </div>)}
            {check && !check.violations.length && <Chip tone="green">No violations</Chip>}
            <div className="cf-actions cf-form-actions">
              <button className="cf-btn" disabled={busy} onClick={() => void perform(async () => {
                setCheck(await request<Check>('/rules/check', actor, {rule: draft}));
              })}>Run check</button>
              <button className="cf-btn primary" disabled={locked || busy || !draft.id.trim()}
                onClick={() => void perform(() => publish(draft))}>Publish{base ? ` v${base.version + 1}` : ''}</button>
            </div>
          </Card>
          {base && <OptimizerPanel key={`${actor}:${base.id}:${base.version}`} actor={actor} ruleId={base.id} draft={draft}
            disabled={busy} onUse={locked || !rules.data?.some(item => item.id === base.id && item.version === base.version)
              ? undefined : next => {
                open({...next, id: base.id, owner: base.owner, origin: base.origin, version: base.version}, base);
                notice('Option loaded into the rule editor');
              }}/>}
        </div>
      </div>
    </>}
  </div>;
}
