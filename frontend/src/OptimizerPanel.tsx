import {useState} from 'react';
import {Link} from 'react-router-dom';
import YAML from 'yaml';
import {request} from './api';
import type {EditableRule} from './console/data';
import {Card, channelName, Chip, countLabel, Failure} from './console/ui';

type Interruptions = {planned_interruptions: number; night_interruptions: number};
type Counterexample = {
  event_id?: number;
  episode_id?: string;
  scenario_id?: string;
  scenario?: {local_time?: string; missing_contact?: string | null};
  note?: string;
  source?: 'labelled_reference' | 'critical_channels' | 'safety_boundary';
  recipient: string;
  channel: string;
  kind: 'missing' | 'delayed';
  delay_s?: number;
};
type PlanChange = {
  event_id: number;
  recipient: string;
  channel: string;
  kind: 'missing' | 'delayed' | 'earlier' | 'added';
  delay_s?: number | null;
};
type Option = {
  id: string;
  draft: EditableRule;
  metrics: Interruptions & {added_delay_p95_s: number | null; added_delay_max_s: number | null; diff_size: number};
  savings: Interruptions & {by_recipient: (Interruptions & {recipient: string})[]};
  diff: string[];
  pair_changes: {counts: Partial<Record<PlanChange['kind'], number>>; examples: PlanChange[]; compared_pairs: number};
  protected_cases: number;
  protected_violations: number;
  acted_on_incidents_affected: (PlanChange & {alert_id: number})[];
  baseline_limitations?: {
    reason: string; recipient?: string; target?: string; channel?: string;
    episode_id?: string; scenario?: {missing_contact?: string | null};
  }[];
  counterexample?: Counterexample | null;
};
type Preview = {
  baseline: Interruptions;
  options: Option[];
  best_rejected: Option | null;
  draft_result?: Option | null;
  evaluated_count: number;
  events_replayed: number;
  config_revision: string | number | null;
  acted_on_incidents_reviewed: number;
  history_truncated: boolean;
  limits: string[];
};
type DraftInput = EditableRule | string;
const protectionLabels = {
  labelled_reference: 'Labelled reference',
  critical_channels: 'Critical channel',
  safety_boundary: 'Safety boundary',
};

function savingsLabel(count: number) {
  if (count === 0) return 'No change';
  return `${countLabel(Math.abs(count))} ${count > 0 ? 'fewer' : 'more'}`;
}

function CounterexampleDetail({example}: {example: Counterexample}) {
  const identity = [
    example.source && protectionLabels[example.source],
    example.event_id != null && `Event #${example.event_id}`,
    example.episode_id && `Episode ${example.episode_id}`,
    example.scenario_id && `Scenario ${example.scenario_id}`,
    example.scenario?.local_time,
    example.scenario?.missing_contact && `Missing contact for ${example.scenario.missing_contact}`,
  ].filter(Boolean).join(' · ');
  const plan = `Planned ${channelName(example.channel)} for ${example.recipient}`;
  return <div className="cf-violation">
    <strong>{plan} {example.kind === 'missing' ? 'would be missing' : 'would be delayed'}
      {example.kind === 'delayed' && example.delay_s != null && ` by ${countLabel(example.delay_s)} s`}</strong>
    {identity && <small>{identity}</small>}
    {example.note && <small>{example.note}</small>}
  </div>;
}

function PlanChangeDetail({change}: {change: PlanChange}) {
  const {channel, recipient, kind, delay_s} = change;
  return <>{channelName(channel)} for {recipient} · {kind}
    {delay_s != null && ` by ${countLabel(Math.abs(delay_s))} s`}</>;
}

function OptionCard({option, baseline, title, onUse}: {
  option: Option; baseline: Interruptions; title: string; onUse?: (draft: EditableRule) => void;
}) {
  const {metrics, savings, protected_cases: cases, protected_violations: violations} = option;
  const baselineLimitations = option.baseline_limitations ?? [];
  const affected = new Set(option.acted_on_incidents_affected.map(row => row.alert_id)).size;
  const {counts, compared_pairs: comparablePairs, examples} = option.pair_changes;
  const pairChanges = Object.entries(counts).map(([kind, count]) => `${countLabel(count)} ${kind}`).join(' · ');
  const rows = [
    {label: 'Planned interruptions', before: baseline.planned_interruptions, after: metrics.planned_interruptions,
      saved: savings.planned_interruptions},
    {label: 'Nighttime planned interruptions', before: baseline.night_interruptions, after: metrics.night_interruptions,
      saved: savings.night_interruptions},
  ];
  return <article className="cf-optimizer-option">
    <h3>{title}</h3>
    <Chip tone={violations ? 'red' : cases ? 'green' : 'neutral'}>
      {violations ? 'Protected violation found' : `0 protected violations in ${countLabel(cases)} ${cases === 1 ? 'case' : 'cases'}`}
    </Chip>
    {option.counterexample && <CounterexampleDetail example={option.counterexample}/>}
    <dl className="cf-comparison cf-optimizer-metrics">
      {rows.map(({label, before, after, saved}) => <div key={label}>
        <dt>{label}</dt>
        <dd>{countLabel(before)} → <strong>{countLabel(after)}</strong><small>{savingsLabel(saved)}</small></dd>
      </div>)}
    </dl>
    <p className="cf-meta">{metrics.added_delay_p95_s == null || metrics.added_delay_max_s == null
      ? 'Added plan delay · no comparable plans'
      : `Added plan delay · p95 ${countLabel(metrics.added_delay_p95_s)} s · max ${countLabel(metrics.added_delay_max_s)} s`}</p>
    <details>
      <summary>Rule diff · {countLabel(metrics.diff_size)} {metrics.diff_size === 1 ? 'change' : 'changes'}</summary>
      {option.diff.length ? <ul>{option.diff.map((line, index) => <li key={index}><code>{line}</code></li>)}</ul>
        : <p>No rule changes.</p>}
    </details>
    <details>
      <summary>Replay evidence · {countLabel(affected)} acted-on {affected === 1 ? 'incident' : 'incidents'} affected
        {baselineLimitations.length > 0 && ` · ${countLabel(baselineLimitations.length)} baseline ${baselineLimitations.length === 1 ? 'limitation' : 'limitations'}`}</summary>
      <p>{countLabel(comparablePairs)} comparable planned routes</p>
      {pairChanges && <p>Changes to planned routes · {pairChanges}</p>}
      {savings.by_recipient.length > 0 && <ul>{savings.by_recipient.map(row => <li key={row.recipient}>
        <strong>{row.recipient}</strong> · {savingsLabel(row.planned_interruptions)} overall · {savingsLabel(row.night_interruptions)} at night
      </li>)}</ul>}
      {option.acted_on_incidents_affected.length > 0 && <ul>{option.acted_on_incidents_affected.map(row =>
        <li key={`${row.alert_id}:${row.event_id}:${row.recipient}:${row.channel}`}>
          <Link to={`/a/${row.alert_id}`}>Incident #{row.alert_id}</Link> · Event #{row.event_id} · <PlanChangeDetail change={row}/>
        </li>
      )}</ul>}
      {examples.length > 0 && <ul>{examples.map(row =>
        <li key={`${row.event_id}:${row.recipient}:${row.channel}`}>Event #{row.event_id} · <PlanChangeDetail change={row}/></li>
      )}</ul>}
      {baselineLimitations.length > 0 && <div>
        <p><strong>Baseline limitations</strong></p>
        <ul>{baselineLimitations.map(({reason, recipient, target, channel, episode_id: episode, scenario}, index) =>
          <li key={index}>{[
            recipient ?? target, channel && channelName(channel),
            scenario?.missing_contact && `Missing contact for ${scenario.missing_contact}`, episode, reason,
          ].filter(Boolean).join(' · ')}</li>
        )}</ul>
      </div>}
    </details>
    {onUse && !violations && <button className="cf-btn" type="button" aria-label={`Use option ${option.id} in editor`}
      onClick={() => onUse(option.draft)}>Use in editor</button>}
  </article>;
}

export function OptimizerPanel({actor, ruleId, proposalId, draft, disabled = false, onUse}: {
  actor: string;
  ruleId: string;
  proposalId?: number;
  draft: DraftInput;
  disabled?: boolean;
  onUse?: (draft: EditableRule) => void;
}) {
  const [replayWindow, setReplayWindow] = useState('24h');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [replay, setReplay] = useState<{data: Preview; input?: DraftInput}>();
  const comparingDraft = replay?.input !== undefined;
  const stale = replay?.input !== undefined && replay.input !== draft;
  const result = stale ? undefined : replay?.data;

  async function preview(compareDraft: boolean) {
    setBusy(true);
    setError(null);
    setReplay(undefined);
    try {
      const input = compareDraft ? draft : undefined;
      const edited: EditableRule | undefined = typeof input === 'string' ? YAML.parse(input) : input;
      if (compareDraft && (edited?.id !== ruleId || !edited.match || !Array.isArray(edited.route) || !edited.route.length)) {
        throw new Error('Provide conditions and recipients for the existing rule ID.');
      }
      const data = await request<Preview>(`/rules/${encodeURIComponent(ruleId)}/optimize/preview`, actor,
        {window: replayWindow, proposal_id: proposalId, draft: edited});
      setReplay({data, input});
    } catch (problem) {
      setError(problem);
    } finally {
      setBusy(false);
    }
  }

  return <Card title="Policy optimizer" className="cf-optimizer">
    <div className="cf-stack">
      <div className="cf-actions cf-optimizer-controls">
        <label className="cf-field">Replay window<select value={replayWindow} disabled={busy || disabled} onChange={event => {
          setReplayWindow(event.target.value);
          setReplay(undefined);
          setError(null);
        }}>
          <option value="1h">Last hour</option>
          <option value="6h">Last 6 hours</option>
          <option value="24h">Last 24 hours</option>
        </select></label>
        <button className="cf-btn" type="button" disabled={busy || disabled} onClick={() => void preview(false)}>Find options</button>
        <button className="cf-btn" type="button" disabled={busy || disabled} onClick={() => void preview(true)}>Replay this draft</button>
      </div>
      <Failure error={error}/>
      {busy && <p role="status">Replaying stored events…</p>}
      {stale && <p role="status">The draft changed. Replay it again to see the updated impact.</p>}
      {result && <>
        <div className="cf-optimizer-scope">
          <p role="status"><strong>{countLabel(result.events_replayed)} events replayed</strong> · {countLabel(result.acted_on_incidents_reviewed)} acted-on incidents reviewed</p>
          <p className="cf-meta">SMS and phone counts · Nighttime 22:00–07:00 Europe/Warsaw</p>
          <details>
            <summary>Replay scope</summary>
            {result.config_revision != null && <p>Config revision <code>{result.config_revision}</code></p>}
            {result.history_truncated && <p>The replay reached its {countLabel(result.events_replayed)}-event sample limit.</p>}
            {result.limits.length > 0 && <ul>{result.limits.map(limit => <li key={limit}>
              {limit.replace(/^Planned, not delivered\. /, '')}
            </li>)}</ul>}
          </details>
        </div>
        {comparingDraft && result.draft_result &&
          <OptionCard option={result.draft_result} baseline={result.baseline} title="Edited draft impact"/>}
        {!comparingDraft && <>
          <p className="cf-meta">{countLabel(result.evaluated_count)} options evaluated</p>
          {result.best_rejected && <OptionCard option={result.best_rejected} baseline={result.baseline}
            title={`Best rejected option ${result.best_rejected.id}`}/>}
          {result.options.length === 0 && <p>No qualifying options in this replay.</p>}
          {result.options.slice(0, 3).map(option => <OptionCard key={option.id} option={option} baseline={result.baseline}
            title={`Option ${option.id}`} onUse={disabled ? undefined : onUse}/>)}
        </>}
      </>}
    </div>
  </Card>;
}
