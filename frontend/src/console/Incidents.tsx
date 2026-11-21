import {priorityLabel} from '../permissions';
import {useState} from 'react';
import {Link} from 'react-router-dom';
import {useQuery} from '@tanstack/react-query';
import {useApp} from '../App';
import {alertListQuery} from '../api';
import {filterAlerts, type Alert} from '../model';
import {ageLabel, Card, Chip, countLabel, Failure, humanize, statusLabel, statusTone, teamName} from './ui';

const views = [
  {key: 'active', label: 'Active', keep: (alert: Alert) => alert.status !== 'resolved'},
  {key: 'open', label: 'Needs ack', keep: (alert: Alert) => alert.status === 'open'},
  {key: 'resolved', label: 'Resolved', keep: (alert: Alert) => alert.status === 'resolved'},
  {key: 'all', label: 'All', keep: () => true},
] as const;
const categories = [
  {value: 'critical', tone: 'red'},
  {value: 'error', tone: 'red'},
  {value: 'warning', tone: 'amber'},
  {value: 'info', tone: 'neutral'},
  {value: 'unknown', tone: 'neutral'},
] as const;

export function Incidents() {
  const {team, directory, feedEnabled} = useApp();
  const list = useQuery(alertListQuery(team, feedEnabled));
  const [view, setView] = useState<(typeof views)[number]['key']>('active');
  const [severity, setSeverity] = useState('');
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState('attention');
  const inView = (list.data ?? []).filter(views.find(item => item.key === view)!.keep);
  const shown = filterAlerts(inView, {team: '', status: '', severity, q: query}).sort((a,b) =>
    sort === 'latest' ? Date.parse(b.first_seen_at)-Date.parse(a.first_seen_at)
    : sort === 'count' ? b.occurrence_count-a.occurrence_count
    : Date.parse(a.first_seen_at)-Date.parse(b.first_seen_at));
  const count = (value: string) => inView.filter(alert => alert.severity === value).length;

  return <div className="cf-stack">
    <Failure error={list.error}/>
    <div className="cf-categories" role="group" aria-label="Severity">
      {categories.map(({value, tone}) => <button type="button" key={value} className={`cf-category cf-${tone}`}
        aria-pressed={severity === value} onClick={() => setSeverity(severity === value ? '' : value)}>
        <strong>{count(value)}</strong><span>{humanize(value)}</span>
      </button>)}
    </div>
    <Card className="cf-incident-list cf-operations-list">
      <div className="cf-card-head">
        <div className="cf-segment" role="group" aria-label="Incident status">
          {views.map(item => <button type="button" key={item.key} aria-pressed={view === item.key}
            onClick={() => setView(item.key)}>{item.label}</button>)}
        </div>
        <input className="cf-search" type="search" placeholder="Search title or rule…" aria-label="Search incidents"
          value={query} onChange={event => setQuery(event.target.value)}/>
        <label className="cf-field cf-inline-sort">Sort by<select value={sort} onChange={event=>setSort(event.target.value)}><option value="attention">Oldest first</option><option value="latest">Latest first</option><option value="count">Occurrences</option></select></label>
        {(severity || query || view !== 'active') && <button className="cf-btn" onClick={()=>{setSeverity('');setQuery('');setView('active');}}>Clear filters</button>}
      </div>
      {shown.map(alert => <Link className="cf-incident-row" key={alert.id} to={`/a/${alert.id}`}>
        <span className={`cf-state-bar cf-${statusTone(alert.status)}`}/>
        <div><strong>{alert.title}</strong><small>{humanize(alert.severity)} · {priorityLabel(alert.priority)} priority · {teamName(alert.team_id, directory)} · <code>INC-{alert.id}</code>
          {alert.rule_id && <> · rule <code>{alert.rule_id}</code></>}
          <span className="cf-mobile-age"> · {ageLabel(alert.first_seen_at)}</span></small></div>
        <div className="cf-list-count"><b className="cf-tabular">{countLabel(alert.occurrence_count)}</b><small>occurrences</small></div>
        <Chip tone={statusTone(alert.status)}>{statusLabel(alert.status)}</Chip>
        <div className="cf-age cf-list-times"><small>First seen <time dateTime={alert.first_seen_at}>{ageLabel(alert.first_seen_at)}</time></small><small>Latest <time dateTime={alert.last_seen_at}>{ageLabel(alert.last_seen_at)}</time></small></div>
      </Link>)}
      {list.isSuccess && !shown.length && <p className="cf-empty-row">No incidents match these filters.</p>}
      {!feedEnabled && <p className="cf-empty-row">Choose who you’re viewing as to see incidents.</p>}
      {list.isPending && feedEnabled && <p className="cf-empty-row">Loading incidents…</p>}
    </Card>
  </div>;
}
