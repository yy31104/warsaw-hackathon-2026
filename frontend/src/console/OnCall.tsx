import {useState, type CSSProperties} from 'react';
import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {useApp} from '../App';
import {request} from '../api';
import type {Directory, Preference} from '../types';
import {channelsAt, daySegments, hhmm, toMinutes} from './preferences';
import {Avatar, Card, channelName, Chip, Failure, personName, teamName, useClock} from './ui';

const channelText = (channels: string[]) => channels.map(channelName).join(' / ') || 'No channel';
const channels = ['phone', 'sms', 'email', 'ui'];
const severities = [
  {value: 'critical', label: 'Critical'},
  {value: 'error', label: 'Error and up'},
  {value: 'warning', label: 'Warning and up'},
  {value: 'info', label: 'Info and up'},
  {value: 'debug', label: 'Everything'},
];
const severityLabel = (value: string) => severities.find(item => item.value === value)?.label ?? value;

function localTime(timezone: string, now: number) {
  return new Intl.DateTimeFormat('en-GB', {timeZone: timezone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).format(now);
}

function Person({id, role, directory, now}: {id: string; role: string; directory: Directory; now: number}) {
  const name = personName(id, directory);
  const user = directory.users.find(candidate => candidate.id === id);
  return <span className="cf-policy-person"><Avatar name={name}/>
    <span><b>{name}</b><small>{role}{user && ` · ${localTime(user.timezone, now)} local`}</small></span>
  </span>;
}

function EscalationPolicies() {
  const {directory, team} = useApp();
  const now = useClock(30000);
  const teams = directory.teams.filter(item => item.oncall?.primary)
    .sort((a, b) => Number(b.id === team) - Number(a.id === team));
  return <Card title="Escalation policies" meta="who gets paged, in order">
    {teams.map(item => {
      const {primary, secondary} = item.oncall!;
      return <div className="cf-policy" key={item.id}>
        <div className="cf-policy-head"><strong>{teamName(item.id, directory)}</strong>
          {!secondary && <Chip tone="amber">No backup</Chip>}</div>
        <ol className="cf-policy-levels">
          <li><span className="cf-policy-level">1</span><Person id={primary} role="Primary" directory={directory} now={now}/>
            <span className="cf-meta">paged first</span></li>
          {secondary && <li><span className="cf-policy-level">2</span><Person id={secondary} role="Backup" directory={directory} now={now}/>
            <span className="cf-meta">{item.escalate_after ? `after ${item.escalate_after} unacknowledged` : 'if nobody acknowledges'}</span></li>}
        </ol>
        {item.approvers.length > 0 && <small className="cf-policy-approvers">
          Approves runbook actions: {item.approvers.map(id => personName(id, directory)).join(', ')}</small>}
      </div>;
    })}
    <p className="cf-meta">Wait times come from each rule’s escalation setting. Teams and on-call people are set in <code>config/teams.yaml</code>.</p>
  </Card>;
}

function TeamDefaults() {
  const {directory} = useApp();
  return <Card title="If none of your rules match" meta="team defaults">
    {directory.default_preferences.map(row => <div className="cf-default-row" key={row.min_severity + (row.when ?? '')}>
      <span>{severityLabel(row.min_severity)}</span>
      <span className="cf-chips">{row.channels.map(channel => <Chip key={channel}>{channelName(channel)}</Chip>)}</span>
    </div>)}
  </Card>;
}

function RuleRow({row, index, count, edit, move, remove}: {
  row: Preference; index: number; count: number;
  edit: (patch: Partial<Preference>) => void; move: () => void; remove: () => void;
}) {
  const [from, until] = row.when ? row.when.split('-') : ['09:00', '18:00'];
  const label = `Rule ${index + 1}`;
  return <li className="cf-pref-row" data-empty={!row.channels.length}>
    <span className="cf-pref-n">{index + 1}</span>
    <div className="cf-pref-when">
      <select aria-label={`${label} · when`} value={row.when ? 'window' : 'any'}
        onChange={event => edit({when: event.target.value === 'window' ? `${from}-${until}` : undefined})}>
        <option value="any">Any time</option><option value="window">Between</option>
      </select>
      {row.when && <span className="cf-pref-times">
        <input type="time" aria-label={`${label} · from`} value={from} required
          onChange={event => event.target.value && edit({when: `${event.target.value}-${until}`})}/>
        <span aria-hidden="true">–</span>
        <input type="time" aria-label={`${label} · until`} value={until} required
          onChange={event => event.target.value && edit({when: `${from}-${event.target.value}`})}/>
      </span>}
    </div>
    <select aria-label={`${label} · severity`} value={row.min_severity} onChange={event => edit({min_severity: event.target.value})}>
      {severities.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
    </select>
    <div className="cf-toggles" role="group" aria-label={`${label} · channels`}>
      {channels.map(channel => <button type="button" key={channel} aria-pressed={row.channels.includes(channel)}
        onClick={() => edit({channels: row.channels.includes(channel)
          ? row.channels.filter(item => item !== channel) : channels.filter(item => item === channel || row.channels.includes(item))})}>
        {channelName(channel)}
      </button>)}
    </div>
    <div className="cf-pref-tools">
      <button type="button" className="cf-icon-btn" aria-label={`Move ${label.toLowerCase()} up`} disabled={!index} onClick={move}>↑</button>
      <button type="button" className="cf-icon-btn" aria-label={`Remove ${label.toLowerCase()}`} disabled={count === 1} onClick={remove}>×</button>
    </div>
  </li>;
}

function Schedule() {
  const {actor, directory, notice} = useApp();
  const client = useQueryClient();
  const now = useClock(10000);
  const user = directory.users.find(candidate => candidate.id === actor);
  const profile = useQuery({
    queryKey: ['preferences', actor],
    queryFn: () => request<Preference[]>(`/users/${encodeURIComponent(actor)}/preferences`, actor),
    enabled: Boolean(user),
  });
  const [changes, setChanges] = useState<Preference[]>();
  const persist = useMutation({
    mutationFn: (updated: Preference[]) => request<Preference[]>(`/users/${encodeURIComponent(actor)}/preferences`, actor, updated, 'PUT'),
    onSuccess: saved => {
      client.setQueryData(['preferences', actor], saved);
      setChanges(undefined);
      notice('Preferences saved');
    },
  });
  const rows = changes ?? profile.data;
  const shown = profile.data;
  if (!user || !rows || !shown) return <Card title="Your notification rules">
    <Failure error={profile.error}/>{!profile.error && <p>Loading preferences…</p>}
  </Card>;
  const selectedTeam = directory.teams.find(team => team.members.includes(actor));
  const relevant = [...rows, ...(selectedTeam?.preferences ?? []), ...directory.default_preferences];
  const active = [...shown, ...(selectedTeam?.preferences ?? []), ...directory.default_preferences];
  const local = localTime(user.timezone, now);
  const minute = toMinutes(local);
  const track = daySegments(relevant);
  const smsIndex = rows.findIndex(row => row.when && row.channels.includes('sms'));
  const range = smsIndex >= 0 ? rows[smsIndex].when!.split('-').map(toMinutes) : null;
  const name = personName(actor, directory);
  const invalid = rows.some(row => !row.channels.length);

  const replace = (next: Preference[]) => setChanges(next);
  const edit = (index: number, patch: Partial<Preference>) => replace(rows.map((row, position) => {
    if (position !== index) return row;
    const merged = {...row, ...patch};
    if (!merged.when) delete merged.when;
    return merged;
  }));
  function slide(position: number, value: number) {
    if (!range) return;
    const times = range.map((current, index) => hhmm(index === position ? value : current));
    edit(smsIndex, {when: times.join('-')});
  }

  return <Card title={name + '’s day'} meta={`Critical · ${user.timezone} · ${local}`}>
    <Failure error={profile.error || persist.error}/>
    <div className="cf-schedule-intro"><Chip>Right now: {channelText(channelsAt(active, minute))}</Chip>
      {changes && <Chip tone="amber">Unsaved</Chip>}</div>
    <div className="cf-day" aria-label="24-hour preference windows">
      {track.map(slot => <div key={slot.start} className={slot.channels.includes('sms') ? 'cf-day-sms' : ''}
        style={{flexGrow: slot.end - slot.start}} title={`${hhmm(slot.start)}–${hhmm(slot.end)} · ${channelText(slot.channels)}`}>
        {channelText(slot.channels)}
      </div>)}
      <span className="cf-day-now" style={{'--position': minute / 1440 * 100 + '%'} as CSSProperties} aria-label={`Now ${local}`}/>
    </div>
    <div className="cf-day-ticks" aria-hidden="true"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>
    {range && <div className="cf-stack cf-schedule-edit">
      {['SMS from', 'SMS until'].map((label, index) => <label className="cf-slider" key={label}>{label}
        <input type="range" min="0" max="1439" step="1" value={range[index]}
          aria-valuetext={hhmm(range[index])} onChange={event => slide(index, Number(event.target.value))}/>
        <output>{hhmm(range[index])}</output>
      </label>)}
    </div>}
    <h3 className="cf-pref-title">Your notification rules <small>first match wins</small></h3>
    <ol className="cf-pref-list">
      {rows.map((row, index) => <RuleRow key={index} row={row} index={index} count={rows.length}
        edit={patch => edit(index, patch)}
        move={() => replace(rows.map((item, position) => position === index - 1 ? rows[index] : position === index ? rows[index - 1] : item))}
        remove={() => replace(rows.filter((_, position) => position !== index))}/>)}
    </ol>
    {invalid && <p className="cf-unknown">Each rule needs at least one channel.</p>}
    <div className="cf-actions cf-form-actions">
      <button className="cf-btn" type="button" onClick={() => replace([...rows, {min_severity: 'critical', channels: ['phone', 'sms']}])}>
        Add rule
      </button>
      <button className="cf-btn primary" disabled={!changes || invalid || persist.isPending} onClick={() => changes && persist.mutate(changes)}>
        Save preferences
      </button>
      {changes && <button className="cf-btn" onClick={() => setChanges(undefined)}>Discard</button>}
    </div>
  </Card>;
}

export function OnCall() {
  const {actor} = useApp();
  return <div className="cf-grid cf-oncall">
    <div className="cf-stack"><EscalationPolicies/><TeamDefaults/></div>
    <Schedule key={actor}/>
  </div>;
}
