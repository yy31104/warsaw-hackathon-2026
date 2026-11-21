import {useState, type CSSProperties} from 'react';
import {Link, useNavigate} from 'react-router-dom';
import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {useApp} from '../App';
import {alertListQuery, api} from '../api';
import type {Alert} from '../model';
import {benchmark} from './benchmark';
import {canReview, detailQuery, orderOpenAlerts, useConsoleQueue, useFunnel, useResponseStats, type Funnel} from './data';
import {ageLabel, Avatar, BenchmarkSource, Card, Chip, countLabel, EscalationRing, Failure, humanize, Icon, LegacyLink,
  EscalationLede, personName, statusLabel, statusTone, teamName, timeLabel} from './ui';

export function IncidentChips({alert}: {alert: Pick<Alert, 'id' | 'team_id' | 'severity'>}) {
  const {directory} = useApp();
  const tone = ['critical', 'error'].includes(alert.severity) ? 'red' : alert.severity === 'warning' ? 'amber' : 'neutral';
  return <div className="cf-chips">
    <Chip tone={tone}>{humanize(alert.severity)}</Chip>
    <Chip>{teamName(alert.team_id, directory)}</Chip>
    <Chip><code>INC-{alert.id}</code></Chip>
  </div>;
}

function FunnelCard({funnel}: {funnel: Funnel}) {
  const values = [funnel.raw, funnel.accepted, funnel.alerts_open, funnel.notifications.delivered];
  const names = ['Events in', 'After dedup', 'Incidents', 'Messages out'];
  const maximum = Math.max(1, ...values);
  const heights = values.map(value => Math.sqrt(value / maximum) * 50);
  const edges = heights.map((height, index) => {
    const midpoint = index * 25 + 12.5;
    return [`${midpoint}% ${50 - height}%`, `${midpoint}% ${50 + height}%`];
  });
  const clip = [`0% ${50 - heights[0]}%`, ...edges.map(edge => edge[0]), `100% ${50 - heights[3]}%`,
    `100% ${50 + heights[3]}%`, ...edges.map(edge => edge[1]).reverse(), `0% ${50 + heights[0]}%`].join(', ');
  return <Card title="The funnel" meta="live · 24 h">
    <div className="cf-funnel-shape" aria-hidden="true" style={{'--funnel-mask': `polygon(${clip})`} as CSSProperties}/>
    <dl className="cf-funnel-labels">{values.map((value, index) => <div key={names[index]}>
      <dd>{countLabel(value)}</dd><dt>{names[index]}</dt>
      {index === 2 && <small>active now</small>}
    </div>)}</dl>
  </Card>;
}

function ResponseTiles({team, enabled}: {team: string; enabled: boolean}) {
  const {data: stats} = useResponseStats(team, enabled);
  if (!stats) return <div className="cf-response-fallback" role="group" aria-label="Response metrics">
    <Card title="Time to notice" meta={<BenchmarkSource file="one-normal/summary.json">{benchmark.label} · N={benchmark.one.n}</BenchmarkSource>}
      className="cf-benchmark-compact">
      <div className="cf-big">{(benchmark.one.p50_ms / 1000).toFixed(3)} <small>s</small></div>
      <span className="cf-meta">p50 · telecom receipts</span>
    </Card>
    <small>More metrics after the first incidents</small>
  </div>;

  const metrics = [
    {key: 'tta_p50_s', label: 'Time to notice', detail: 'Median', unit: 's'},
    {key: 'mtta_s', label: 'Time to respond', detail: 'Average · acknowledged', unit: 's'},
    {key: 'mttr_s', label: 'Time to fix', detail: 'Average · resolved', unit: 's'},
    {key: 'notifications_per_alert', label: 'Messages per incident', detail: '', unit: ''},
  ] as const;
  return <div className="cf-response-tiles" role="group" aria-label="Response metrics">
    {metrics.map(({key, label, detail, unit}) => {
      const value = stats[key];
      const count = stats.sample_counts?.[key] ?? (key === 'notifications_per_alert' ? stats.n_alerts : undefined);
      const caption = [value == null ? 'No samples' : detail, count == null ? '' : `N=${countLabel(count)}`]
        .filter(Boolean).join(' · ');
      return <Card title={label} key={key} meta="live · 24 h">
        <div className="cf-big">{value == null ? '—' : <>{value.toLocaleString('en-GB', {maximumFractionDigits: 3})}
          {unit && <small> {unit}</small>}</>}</div>
        <span className="cf-meta">{caption}</span>
      </Card>;
    })}
  </div>;
}

export function Now() {
  const {actor, team, directory, feedEnabled, notice} = useApp();
  const {open, pending, actions} = useConsoleQueue(team, feedEnabled);
  const all = useQuery(alertListQuery(team, feedEnabled));
  const funnel = useFunnel();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [filter, setFilter] = useState('Open');
  const hero = orderOpenAlerts(open.data ?? [])[0];
  const heroDetail = useQuery(detailQuery(hero?.id));
  const incidents = (all.data ?? []).filter(alert => filter === 'All'
    || (filter === 'Resolved' ? alert.status === 'resolved' : alert.status !== 'resolved'));
  const acknowledge = useMutation({
    mutationFn: (id: number) => api.transition(id, 'ack', actor),
    onSuccess: async (_, id) => {
      await client.invalidateQueries({queryKey: ['alerts']});
      notice('Incident acknowledged');
      navigate(`/a/${id}`);
    },
  });

  return <div className="cf-stack">
    <Failure error={open.error || all.error || funnel.error || pending.error || acknowledge.error}/>
    {hero ? <Card className="cf-hero">
      <div className="cf-hero-copy">
        <div className="cf-kicker"><span className="cf-state-dot"/>Needs you now</div>
        <h2>{hero.title}</h2>
        {heroDetail.data && <EscalationLede incident={heroDetail.data} directory={directory}/>}
        <IncidentChips alert={hero}/>
      </div>
      <div className="cf-hero-end">
        <div className="cf-event-total"><strong>×{countLabel(hero.occurrence_count)}</strong><small>events · live</small></div>
        {heroDetail.data && <EscalationRing incident={heroDetail.data} directory={directory}/>}
        <div className="cf-actions">
          <button className="cf-btn primary" disabled={acknowledge.isPending} onClick={() => acknowledge.mutate(hero.id)}>
            <Icon name="check"/>Acknowledge
          </button>
          <Link className="cf-btn" to={`/a/${hero.id}`}>Open</Link>
        </div>
      </div>
    </Card> : open.isSuccess ? <Card className="cf-all-handled">
      <Icon name="check"/><h2>All handled</h2>
    </Card> : <Card><p>{feedEnabled ? 'Loading incidents…' : 'Choose a team member.'}</p></Card>}
    <ResponseTiles team={team} enabled={feedEnabled}/>
    {funnel.data && <FunnelCard funnel={funnel.data}/>}
    <div className="cf-grid cf-grid-wide">
      <Card className="cf-incident-list">
        <div className="cf-card-head">
          <h2>Incidents</h2>
          <div className="cf-segment" role="group" aria-label="Incident status">
            {['Open', 'Resolved', 'All'].map(value => <button key={value} aria-pressed={filter === value}
              onClick={() => setFilter(value)}>{value}</button>)}
          </div>
        </div>
        {incidents.map(alert => <Link className="cf-incident-row" key={alert.id} to={`/a/${alert.id}`}>
          <span className={`cf-state-bar cf-${statusTone(alert.status)}`}/>
          <div><strong>{alert.title}</strong><small>{teamName(alert.team_id, directory)} · <code>INC-{alert.id}</code>
            <span className="cf-mobile-age"> · {ageLabel(alert.first_seen_at)}</span></small></div>
          <b className="cf-tabular">×{countLabel(alert.occurrence_count)}</b>
          <Chip tone={statusTone(alert.status)}>{statusLabel(alert.status)}</Chip>
          <time className="cf-age" dateTime={alert.first_seen_at}>{ageLabel(alert.first_seen_at)}</time>
        </Link>)}
        {all.isSuccess && !incidents.length && <p className="cf-empty-row">No incidents here.</p>}
        <div className="cf-foot cf-actions"><LegacyLink to="/incidents">All incidents</LegacyLink><LegacyLink to="/overview">Overview</LegacyLink></div>
      </Card>
      <div className="cf-stack">
        <Card title="Waiting for approval" meta={pending.isSuccess ? `${pending.data.pending_approval_count} · live` : undefined}>
          {pending.data?.pending_approval_count === 0 && <p><Icon name="check"/> Nothing waiting</p>}
          {actions.map(({alert, action}) => <div className="cf-side-row" key={action.run_id}>
            <Icon name="shield"/><div><strong>{humanize(action.action)}</strong>
              <small>{personName(action.requested_by, directory)} · {timeLabel(action.created_at)}</small>
            </div>
            <Link className="cf-btn" to={`/a/${alert.id}#runbook`}>{canReview(action, actor) ? 'Review' : 'View'}</Link>
          </div>)}
          <LegacyLink to="/approvals">All approvals</LegacyLink>
        </Card>
        <Card title="On call now">
          {directory.teams.flatMap(item => {
            if (!item.oncall?.primary) return [];
            const name = personName(item.oncall.primary, directory);
            return <div className="cf-side-row" key={item.id}><Avatar name={name}/><strong>{name}</strong>
              <span className="cf-meta">{teamName(item.id, directory)}</span></div>;
          })}
          <LegacyLink to="/oncall">Who’s on call</LegacyLink>
        </Card>
      </div>
    </div>
  </div>;
}
