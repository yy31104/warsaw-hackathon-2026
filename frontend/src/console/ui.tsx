import {useEffect, useState, type CSSProperties, type ReactNode} from 'react';
import {Link} from 'react-router-dom';
import type {Directory} from '../types';
import type {IncidentDetail, Problem} from './data';

const paths = {
  funnel: 'M3 5h18l-7 8v6l-4 2v-8z',
  now: 'M3 12h4l3-8 4 16 3-8h4',
  sources: 'M12 3v12m0 0-4-4m4 4 4-4M4 17v3h16v-3',
  rules: 'M4 6h10m3 0h3M4 12h3m3 0h10M4 18h10m3 0h3',
  runbooks: 'M5 4h14v16H5zM9 8h6M9 12h6M9 16h4',
  oncall: 'M12 8a3 3 0 1 1-6 0 3 3 0 0 1 6 0ZM3 20a6 6 0 0 1 12 0M16 5a3 3 0 0 1 0 6M21 20a5 5 0 0 0-3-4',
  health: 'M20 12h-4l-3 8-4-16-3 8H2',
  intro: 'm8 5 11 7-11 7z',
  check: 'm5 12 4 4 10-10',
  shield: 'M12 22c5-3 8-6 8-11V5l-8-3-8 3v6c0 5 3 8 8 11Z',
  clock: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM12 7v5l3 2',
  phone: 'M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2',
  arrow: 'M19 12H5m0 0 6-6m-6 6 6 6',
} as const;

export function Icon({name}: {name: keyof typeof paths}) {
  return <svg className="cf-icon" viewBox="0 0 24 24" aria-hidden="true"><path d={paths[name]}/></svg>;
}

export function Chip({tone = 'neutral', children}: {tone?: 'red' | 'amber' | 'green' | 'neutral'; children: ReactNode}) {
  return <span className={`cf-chip cf-${tone}`}>{children}</span>;
}

export const statusLabel = (status: string) => status === 'acknowledged' ? 'Acked' : status === 'resolved' ? 'Resolved' : 'Open';
export const statusTone = (status: string) => status === 'resolved' ? 'green' : status === 'acknowledged' ? 'amber' : 'red';
export const capitalize = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);
export const humanize = (value: string) => capitalize(value.replace(/[_-]/g, ' '));
export const channelName = (value: string) => ['sms', 'ui'].includes(value) ? value.toUpperCase() : humanize(value);

export function personName(id: string, directory: Directory) {
  return directory.users.find(user => user.id === id)?.name.split(' (')[0] ?? capitalize(id);
}

export function teamName(id: string, directory: Directory) {
  return directory.teams.find(team => team.id === id)?.name?.replace(/^Team /, '') ?? humanize(id);
}

export function Avatar({name}: {name: string}) {
  return <span className="cf-avatar" aria-hidden="true">{name.slice(0, 1)}</span>;
}

export function useClock(period = 1000) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), period);
    return () => window.clearInterval(timer);
  }, [period]);
  return now;
}

const warsaw = new Intl.DateTimeFormat('en-GB', {timeZone: 'Europe/Warsaw', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
export const timeLabel = (input: string | number) => warsaw.format(typeof input === 'string' ? Date.parse(input) : input);
export const countLabel = (input: number) => input.toLocaleString('en-GB');

export function ageLabel(input: string, end = Date.now()) {
  const seconds = Math.max(0, Math.floor((end - Date.parse(input)) / 1000));
  if (seconds < 60) return seconds + 's';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return minutes + 'm';
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? hours + 'h' : Math.floor(hours / 24) + 'd';
}

export function Card({title, meta, children, className = ''}: {
  title?: ReactNode; meta?: ReactNode; children: ReactNode; className?: string;
}) {
  return <section className={`cf-card ${className}`}>
    {(title || meta) && <div className="cf-card-head"><h2>{title}</h2>{meta && <span className="cf-meta">{meta}</span>}</div>}
    {children}
  </section>;
}

export function Failure({error}: {error: unknown}) {
  return error ? <div className="cf-error" role="alert">{error instanceof Error ? error.message : String(error)}</div> : null;
}

export function Problems({items, suggest}: {items?: Problem[]; suggest?: (from: string, to: string) => void}) {
  if (!items?.length) return null;
  return <ul className="cf-problems">{items.map((problem, index) => <li key={index}>
    {problem.path && <code>{problem.path}</code>} <span>{problem.message}</span>
    {problem.fix_example != null && <pre>{typeof problem.fix_example === 'string'
      ? problem.fix_example : JSON.stringify(problem.fix_example, null, 2)}</pre>}
    {problem.suggestions?.map(path => suggest && problem.path
      ? <button className="cf-btn" key={path} onClick={() => suggest(problem.path!, path)}>Did you mean {path}</button>
      : <code key={path}>{path}</code>)}
  </li>)}</ul>;
}

export function EscalationLede({incident, directory}: {incident: IncidentDetail; directory: Directory}) {
  if (incident.status === 'resolved') return <p className="cf-lede">Resolved. Escalation has stopped.</p>;
  if (incident.status === 'acknowledged') {
    return <p className="cf-lede">Escalation stopped. Nobody else will be woken up.</p>;
  }
  const next = incident.next_escalation;
  if (next === undefined) return null;
  if (!next) return <p className="cf-lede">No escalation is scheduled.</p>;

  const names = next.targets.map(id => personName(id, directory));
  const action = names.length ? `${names.join(', ')} ${names.length === 1 ? 'is' : 'are'} paged` : 'Escalation is scheduled';
  return <p className="cf-lede">{action} at <time dateTime={next.at}>{timeLabel(next.at)}</time> unless someone takes it.</p>;
}

export function EscalationRing({incident, directory}: {incident: IncidentDetail; directory: Directory}) {
  const now = useClock();
  const next = incident.next_escalation;
  if (next === undefined) return null;
  if (!next) return incident.status === 'open'
    ? <span className="cf-meta">No escalation scheduled</span>
    : <div className="cf-stopped">
      <Icon name="check"/><div><strong>Escalation stopped</strong>
        {incident.acked_by && incident.acked_at && <small>{personName(incident.acked_by, directory)} · {timeLabel(incident.acked_at)}</small>}
      </div>
    </div>;
  const at = Date.parse(next.at);
  const remaining = Math.max(0, Math.ceil((at - now) / 1000));
  const total = Math.max(1, (at - Date.parse(incident.first_seen_at)) / 1000);
  const ratio = Math.min(1, remaining / total);
  const label = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}`;
  const targets = next.targets.map(id => personName(id, directory));
  return <div className="cf-next">
    <div className="cf-ring" style={{'--progress': ratio * 100 + '%'} as CSSProperties}>
      <time dateTime={next.at} aria-label={`Escalation at ${timeLabel(next.at)} Warsaw`}>{label}</time>
    </div>
    {targets.length > 0 && <div><small>then</small><strong>{targets.join(', ')} {targets.length === 1 ? 'is' : 'are'} paged</strong>
      {next.channels?.length > 0 && <small>{next.channels.join(' + ')}</small>}</div>}
  </div>;
}

export function BenchmarkSource({children, file}: {children: ReactNode; file: string}) {
  const base = 'https://github.com/yy31104/warsaw-hackathon-2026/blob/10c398e/loadtest/results/platform-bench/';
  return <a className="cf-source" href={base + file}>{children}</a>;
}

export function LegacyLink({to, children}: {to: string; children: ReactNode}) {
  return <Link className="cf-text-link" to={to}>{children}</Link>;
}
