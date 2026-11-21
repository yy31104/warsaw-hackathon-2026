import {useState} from 'react';
import {Link} from 'react-router-dom';
import {DEFAULT_THEME, getThemeColor} from '@mantine/core';
import type {Alert} from './model';
import {priorityLabel} from './permissions';

const severityOrder = ['critical','error','warning','info','debug','unknown'];
export function ruleAccent(color: string) {
  const names: Record<string,string> = {amber: 'yellow', grey: 'gray'};
  return getThemeColor(names[color] ?? color, DEFAULT_THEME);
}
export function SeverityBadge({value}: {value: string}) {
  return <span className="alert-severity" data-level={value}>{value}</span>;
}
function severityRank(severity: string) {
  const index = severityOrder.indexOf(severity);
  return index < 0 ? severityOrder.length : index;
}
export function orderAlerts(alerts: Alert[]) {
  return [...alerts].sort((a,b) => severityRank(a.severity) - severityRank(b.severity)
    || (a.priority ?? 'P9').localeCompare(b.priority ?? 'P9')
    || (b.score ?? -1) - (a.score ?? -1));
}

export function ActiveAlerts({items, message}: {items: Alert[]; message?: string}) {
  const [filter,setFilter] = useState('all');
  const visible = orderAlerts(items).filter(a => filter === 'all' || (filter === 'urgent'
    ? ['critical','error'].includes(a.severity) : a.severity === 'warning'));
  return <section className="queue-box" aria-labelledby="active-heading">
    <div className="box-heading"><h2 id="active-heading">Alert queue</h2>
      <Link className="small-link" to="/alerts?status=active">View all <span aria-hidden="true">→</span></Link></div>
    <div className="queue-tabs" aria-label="Filter active alerts" role="group">
      {[['all','All'],['urgent','Critical & errors'],['warning','Warnings']].map(([id,label]) =>
        <button type="button" key={id} aria-pressed={filter === id} onClick={() => setFilter(id)}>{label}</button>)}
    </div>
    {message ? <p className="queue-message">{message}</p> : visible.length ? <div className="queue-items">
      {visible.slice(0,5).map(alert => <Link className="queue-item" key={alert.id} to={`/a/${alert.id}`}
        style={{borderInlineStartColor: ruleAccent(alert.color)}}>
        <div><div className="queue-meta"><SeverityBadge value={alert.severity}/>
          <span>{alert.team_id} · {alert.status}</span></div>
          <strong>{alert.title}</strong><small>{priorityLabel(alert.priority)} priority · #{alert.id}</small>
        </div>
        <div className="queue-count"><b>{alert.occurrence_count.toLocaleString()}</b><span>{alert.occurrence_count === 1 ? 'occurrence' : 'occurrences'}</span></div>
      </Link>)}
      {visible.length > 5 && <p className="queue-limit">Showing 5 of {visible.length} alerts, highest severity first.</p>}
    </div> : <div className="queue-empty">
      <span className="empty-tray" aria-hidden="true"><i/></span>
      <strong>{items.length ? 'No alerts match this filter' : 'No active alerts'}</strong>
      <p>{items.length ? 'Choose All to see the other alerts.' : 'New alerts for your team will appear here.'}</p>
    </div>}
  </section>;
}
