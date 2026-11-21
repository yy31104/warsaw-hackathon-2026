import {Link} from 'react-router-dom';
import type {Alert} from './model';

export function StatusOverview({alerts}: {alerts?: Alert[]}) {
  const metrics = [
    {label: 'Active alerts', value: alerts?.length, to: '/alerts?status=active', tone: 'all'},
    {label: 'Critical alerts', value: alerts?.filter(a => a.severity === 'critical').length, to: '/alerts?status=active&severity=critical', tone: 'critical'},
    {label: 'Acknowledged', value: alerts?.filter(a => a.status === 'acknowledged').length, to: '/alerts?status=acknowledged', tone: 'ack'},
    {label: 'Occurrences', value: alerts?.reduce((total, a) => total + a.occurrence_count, 0), tone: 'count'},
  ];
  return <section className="metrics-strip" aria-label="Current team alert totals">
    {metrics.map(metric => {
      const body = <><span className="metric-name"><i data-tone={metric.tone}/>{metric.label}</span>
        <strong>{metric.value?.toLocaleString() ?? '—'}</strong></>;
      return metric.to ? <Link key={metric.label} to={metric.to} className="metric-cell">{body}</Link>
        : <div key={metric.label} className="metric-cell" title="Distinct failure occurrences across active alerts">{body}</div>;
    })}
  </section>;
}
