import {useState, type CSSProperties} from 'react';
import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {useApp} from '../App';
import {request} from '../api';
import type {Preference} from '../types';
import {channelsAt, daySegments, hhmm, toMinutes} from './preferences';
import {Avatar, Card, channelName, Chip, Failure, LegacyLink, personName, teamName, useClock} from './ui';

const channelText = (channels: string[]) => channels.map(channelName).join(' / ') || 'No channel';

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
  if (!user || !rows || !shown) return <Card title="Preferences">
    <Failure error={profile.error}/>{!profile.error && <p>Loading preferences…</p>}
  </Card>;
  const selectedTeam = directory.teams.find(team => team.members.includes(actor));
  const relevant = [...rows, ...(selectedTeam?.preferences ?? []), ...directory.default_preferences];
  const active = [...shown, ...(selectedTeam?.preferences ?? []), ...directory.default_preferences];
  const local = new Intl.DateTimeFormat('en-GB', {timeZone: user.timezone, hour: '2-digit',
    minute: '2-digit', hourCycle: 'h23'}).format(now);
  const minute = toMinutes(local);
  const track = daySegments(relevant);
  const smsIndex = rows.findIndex(row => row.when && row.channels.includes('sms'));
  const range = smsIndex >= 0 ? rows[smsIndex].when!.split('-').map(toMinutes) : null;
  const name = personName(actor, directory);

  function update(position: number, minute: number) {
    if (!range) return;
    const times = range.map((value, index) => hhmm(index === position ? minute : value));
    setChanges(rows?.map((row, index) => index === smsIndex ? {...row, when: times.join('-')} : row));
  }

  return <Card title={name + '’s day'} meta={`Critical · ${user.timezone}`}>
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
          aria-valuetext={hhmm(range[index])} onChange={event => update(index, Number(event.target.value))}/>
        <output>{hhmm(range[index])}</output>
      </label>)}
      <div className="cf-actions">
        <button className="cf-btn primary" disabled={!changes || persist.isPending} onClick={() => changes && persist.mutate(changes)}>
          Save preferences
        </button>
        {changes && <button className="cf-btn" onClick={() => setChanges(undefined)}>Discard</button>}
      </div>
    </div>}
    <div className="cf-form-actions"><LegacyLink to="/people">All preferences</LegacyLink></div>
  </Card>;
}

export function OnCall() {
  const {actor, directory} = useApp();
  return <div className="cf-stack">
    <div className="cf-grid">
      <Card title="Who gets paged" meta="live">
        {directory.teams.flatMap(item => {
          if (!item.oncall?.primary) return [];
          const primary = personName(item.oncall.primary, directory);
          const backup = item.oncall.secondary && personName(item.oncall.secondary, directory);
          return <div className="cf-ladder" key={item.id}>
            <strong>{teamName(item.id, directory)}</strong>
            <div className="cf-ladder-people">
              <span><Avatar name={primary}/><span><b>{primary}</b><small>Primary</small></span></span>
              {backup && <><span className="cf-ladder-arrow">→ {item.escalate_after && <small>{item.escalate_after}</small>}</span>
                <span><Avatar name={backup}/><span><b>{backup}</b><small>Backup</small></span></span></>}
            </div>
          </div>;
        })}
      </Card>
      <Schedule key={actor}/>
    </div>
  </div>;
}
