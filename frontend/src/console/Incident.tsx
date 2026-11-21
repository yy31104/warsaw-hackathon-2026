import {useEffect, useState, type CSSProperties} from 'react';
import {useLocation, useParams} from 'react-router-dom';
import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {useApp} from '../App';
import {api, request} from '../api';
import {CheckReceipt} from '../CheckReceipt';
import {inTeam} from '../permissions';
import {canReview, detailQuery, representativeExplainPath, type IncidentDetail} from './data';
import {Card, Chip, EscalationLede, EscalationRing, Failure, humanize, Icon, LegacyLink,
  personName, teamName, countLabel, timeLabel, useClock} from './ui';
import {IncidentChips} from './Now';
import {timelineRows, type TimelineRule} from './timeline';

function Timeline({incident}: {incident: IncidentDetail}) {
  const {directory} = useApp();
  const notifications = useQuery({queryKey: ['detail', 'notifications', incident.id],
    queryFn: () => api.notifications(incident.id)});
  const rules = useQuery({queryKey: ['rules'],
    queryFn: async () => (await request<{items: TimelineRule[]}>('/rules')).items});
  const rows = timelineRows(incident.timeline, notifications.data ?? [], rules.data ?? [], directory);
  return <Card title="Timeline" meta="Warsaw">
    {rows.length === 0 && <p>No activity yet.</p>}
    <ol className="cf-timeline">{rows.map((entry, index) => <li key={index}>
      <Icon name={entry.kind === 'notification' ? 'phone' : entry.kind === 'audit' ? 'check' : 'now'}/>
      <span>{entry.text}</span><time dateTime={entry.ts}>{timeLabel(entry.ts)}</time>
    </li>)}</ol>
  </Card>;
}

type Explain = {decisions: {rule_id: string; alert_id: number | null;
  reason?: {matched?: boolean; trace?: {field: string; actual: unknown; matched: boolean}[]}}[]};

function WhyYou({incident}: {incident: IncidentDetail}) {
  const {actor, directory} = useApp();
  const path = representativeExplainPath(incident);
  const explain = useQuery({queryKey: ['detail', 'explain', incident.representative_event_id],
    queryFn: () => request<Explain>(path!), enabled: Boolean(path)});
  const notifications = useQuery({queryKey: ['detail', 'notifications', incident.id],
    queryFn: () => api.notifications(incident.id), enabled: Boolean(path)});
  if (!path) return null;

  const decision = explain.data?.decisions.find(item => item.alert_id === incident.id && item.rule_id === incident.rule_id);
  const deciding = decision?.reason?.trace?.filter(item => item.matched) ?? [];
  const received = notifications.data?.filter(item => item.recipient === actor && item.kind !== 'approval') ?? [];
  return <Card title="Why you">
    <Failure error={explain.error || notifications.error}/>
    <ol className="cf-chain">
      {decision && <li><strong>{humanize(decision.rule_id)}</strong>
        {deciding.slice(0, 3).map((field, index) => <small key={index}><code>{field.field}</code> = {String(field.actual)}</small>)}</li>}
      <li><strong>{teamName(incident.team_id, directory)}</strong><small>Incident team</small></li>
      {received.length > 0 && <li><strong>{personName(actor, directory)}</strong>
        {received.map(message => {
          const entry = incident.timeline.find(item => item.kind === 'notification'
            && item.text === `${message.channel} → ${message.recipient}: delivered`);
          return <small key={message.id}>{message.channel.toUpperCase()} · {message.state}
            {message.state === 'delivered' && entry && ` · ${timeLabel(entry.ts)}`}</small>;
        })}</li>}
    </ol>
  </Card>;
}

function EventBars({incident}: {incident: IncidentDetail}) {
  const now = useClock(30000);
  const minute = Math.floor(now / 60000);
  const bars = Array.from({length: 10}, () => 0);
  for (const occurrence of incident.occurrences) {
    const index = Math.floor(Date.parse(occurrence.seen_at) / 60000) - minute + 9;
    if (index >= 0 && index < bars.length) bars[index]++;
  }
  const maximum = Math.max(...bars);
  return <Card title="Events" meta="live">
    <div className="cf-big">×{countLabel(incident.occurrence_count)}</div>
    {maximum > 0 && <>
      <div className="cf-bars" role="img" aria-label="Recorded occurrences per minute">
        {bars.map((count, index) => <i key={index} style={{'--bar': count / maximum * 100 + '%'} as CSSProperties}
          title={`${timeLabel((minute - 9 + index) * 60000)} · ${count}`}/>)}
      </div>
      <span className="cf-meta">Last 10 min · N={bars.reduce((sum, count) => sum + count, 0)}</span>
    </>}
  </Card>;
}

function IncidentRunbook({incident, canAct}: {incident: IncidentDetail; canAct: boolean}) {
  const {actor, directory} = useApp();
  const client = useQueryClient();
  const id = incident.id;
  const runbook = useQuery({queryKey: ['detail', 'runbook', id], queryFn: () => api.runbook(id)});
  const action = useMutation({
    mutationFn: ({path, body = {}}: {path: string; body?: Record<string, unknown>}) => request(path, actor, body),
    onSuccess: async () => {
      await Promise.all([client.invalidateQueries({queryKey: ['detail']}), client.invalidateQueries({queryKey: ['alerts']}),
        client.invalidateQueries({queryKey: ['pending-approvals']})]);
    },
  });
  return <div id="runbook">
    <Card title="Runbook" meta={runbook.data?.runbook_id}>
      <Failure error={runbook.error || action.error}/>
      {runbook.isPending && <p>Loading steps…</p>}
      {runbook.data === null && <p>No runbook attached.</p>}
      {runbook.data?.steps.map(step => {
        const pending = incident.pending_actions?.find(item => item.run_id === step.run_id);
        const awaiting = step.state === 'pending_approval' || Boolean(pending);
        const approvers = (pending?.approvers ?? step.approvers ?? []).filter(person => person !== pending?.requested_by);
        const readOnly = step.kind === 'check' || step.action === 'http_get';
        const kind = step.approval === 'required' ? 'Needs approval' : readOnly ? 'Read-only'
          : step.kind === 'manual' ? 'Manual' : step.kind === 'link' ? 'Fallback' : 'Action';
        const state = step.state === 'unknown' ? 'Unknown · retry locked' : step.state ? humanize(step.state) : null;
        const runnable = Boolean(step.action) && (!step.state || ['expired', 'rejected'].includes(step.state));
        const url = step.kind === 'link' && 'url' in step && typeof step.url === 'string' && /^https?:\/\//i.test(step.url)
          ? step.url : undefined;
        return <div className="cf-step" key={step.index}>
          <div className={`cf-step-icon cf-${awaiting ? 'amber' : step.state === 'succeeded' ? 'green' : 'neutral'}`}>
            <Icon name={step.approval === 'required' ? 'shield' : readOnly ? 'check' : 'runbooks'}/>
          </div>
          <div className="cf-step-copy"><strong>{url ? <a href={url}>{step.text}</a> : step.text}</strong>
            <small>{kind}{step.approved_by && ` · Approved by ${personName(step.approved_by, directory)}`}</small>
            {!awaiting && state && <span className={step.state === 'unknown' ? 'cf-unknown' : 'cf-meta'}>{state}</span>}
            {awaiting && (!pending || !canReview(pending, actor)) && <Chip tone="amber">
              {approvers.length ? `Waiting for ${approvers.map(person => personName(person, directory)).join(', ')}` : 'Waiting for approval'}
            </Chip>}
          </div>
          <div className="cf-actions">
            {canAct && incident.status !== 'resolved' && !awaiting && runnable && <button className="cf-btn"
              disabled={action.isPending} onClick={() => action.mutate({path: `/alerts/${id}/actions`, body: {step_index: step.index}})}>
              {step.approval === 'required' ? 'Request approval' : 'Run'}
            </button>}
            {pending && canReview(pending, actor) && <>
              <button className="cf-btn" disabled={action.isPending}
                onClick={() => action.mutate({path: `/action-runs/${pending.run_id}/approve`})}>Approve</button>
              <button className="cf-btn" disabled={action.isPending}
                onClick={() => action.mutate({path: `/action-runs/${pending.run_id}/reject`})}>Reject</button>
            </>}
            {step.state === 'unknown' && <CheckReceipt key={`${actor}-${step.run_id}`} step={step} actor={actor}/>}
            {canAct && step.run_id && step.state === 'failed' && <button className="cf-btn"
              disabled={action.isPending} onClick={() => action.mutate({path: `/action-runs/${step.run_id}/check-result`})}>Check result</button>}
          </div>
        </div>;
      })}
    </Card>
  </div>;
}

export function Incident() {
  const {id} = useParams();
  const location = useLocation();
  const number = Number(id);
  const {actor, directory, notice} = useApp();
  const client = useQueryClient();
  const detail = useQuery(detailQuery(Number.isSafeInteger(number) && number > 0 ? number : undefined));
  const incident = detail.data;
  const [proposing, setProposing] = useState(false);
  const [note, setNote] = useState('');
  const transition = useMutation({
    mutationFn: (next: 'ack' | 'resolve') => api.transition(number, next, actor),
    onSuccess: async () => {
      await client.invalidateQueries({queryKey: ['alerts']});
      void client.invalidateQueries({queryKey: ['console-funnel']});
    },
  });
  const propose = useMutation({
    mutationFn: () => request('/proposals', actor, {alert_id: number, text: note}),
    onSuccess: () => {
      setProposing(false);
      setNote('');
      notice('Proposal sent');
      void client.invalidateQueries({queryKey: ['proposals']});
    },
  });
  useEffect(() => {
    if (incident && location.hash === '#runbook') document.getElementById('runbook')?.scrollIntoView();
  }, [incident, location.hash]);
  if (!Number.isSafeInteger(number) || number <= 0) return <Card title="Incident unavailable"><LegacyLink to="/">Back to Now</LegacyLink></Card>;
  if (detail.error) return <Failure error={detail.error}/>;
  if (!incident || !directory.users.length) return <p>Loading incident…</p>;
  const canAct = inTeam(incident.team_id, actor, false, directory);
  const approver = directory.teams.some(team => team.id === incident.team_id && team.approvers.includes(actor));
  if (!canAct && !approver) return <Card title="Incident unavailable"><p>Switch to an incident team member.</p></Card>;

  const lifecycle = [
    {name: 'Opened', at: incident.first_seen_at, tone: 'red'},
    {name: 'Acked', at: incident.acked_at, tone: 'amber'},
    {name: 'Resolved', at: incident.resolved_at, tone: 'green'},
  ];
  return <div className="cf-stack">
    <Card className="cf-incident-header">
      <IncidentChips alert={incident}/>
      <h2>{incident.title}</h2>
      <EscalationLede incident={incident} directory={directory}/>
      {incident.body && <p className="cf-incident-summary">{incident.body}</p>}
      <div className="cf-lifecycle-wrap">
        <ol className="cf-lifecycle">{lifecycle.map(stage => <li key={stage.name} className={`cf-${stage.tone}`} data-completed={Boolean(stage.at)}>
          <span className="cf-state-dot"/><div><strong>{stage.name}</strong>
            {stage.at && <time dateTime={stage.at}>{timeLabel(stage.at)}</time>}</div>
        </li>)}</ol>
        <div className="cf-actions">
          {incident.status === 'open' && canAct && <button className="cf-btn primary" disabled={transition.isPending}
            onClick={() => transition.mutate('ack')}><Icon name="check"/>Acknowledge</button>}
          {incident.status !== 'resolved' && canAct && <button className={`cf-btn ${incident.status === 'acknowledged' ? 'primary' : ''}`}
            disabled={transition.isPending} onClick={() => transition.mutate('resolve')}>Resolve</button>}
          {incident.status === 'resolved' && <Chip tone="green">Resolved</Chip>}
        </div>
      </div>
      <Failure error={transition.error}/>
    </Card>
    <div className="cf-grid cf-tiles">
      <EventBars incident={incident}/>
      {incident.next_escalation !== undefined && <Card title="Next"><EscalationRing incident={incident} directory={directory}/></Card>}
      <WhyYou incident={incident}/>
    </div>
    <div className="cf-grid cf-grid-wide">
      <div className="cf-stack">
        <IncidentRunbook incident={incident} canAct={canAct}/>
        {!!Object.keys(incident.fields).length && <Card title="Key fields">
          <div className="cf-chips">{Object.entries(incident.fields).map(([field, value]) => <Chip key={field}>
            <code>{field}</code> {typeof value === 'string' ? value : JSON.stringify(value)}
          </Chip>)}</div>
        </Card>}
      </div>
      <Timeline incident={incident}/>
    </div>
    <div className="cf-actions">
      <LegacyLink to={`/a/${incident.id}/details`}>More details</LegacyLink>
      {canAct && <button className="cf-btn" onClick={() => setProposing(value => !value)}>Propose fix</button>}
    </div>
    {proposing && <Card title="Propose fix">
      <label className="cf-field">What should change?<textarea value={note} onChange={event => setNote(event.target.value)} rows={3}/></label>
      <button className="cf-btn" disabled={!note.trim() || propose.isPending} onClick={() => propose.mutate()}>Send proposal</button>
      <Failure error={propose.error}/>
    </Card>}
  </div>;
}
