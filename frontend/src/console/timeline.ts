import type {Directory} from '../types';
import type {IncidentDetail} from './data';
import {personName, teamName} from './ui';

type Entry = IncidentDetail['timeline'][number];
type Notification = {
  channel: string; recipient: string; state: string;
  attempt_log: {outcome: string; latency_ms?: number | null}[];
};
export type TimelineRule = {id: string; name?: string};

const channels: Record<string, string> = {phone: 'Call', sms: 'SMS', email: 'Email', ui: 'In-app message'};
const delivery: Record<string, string> = {
  delivered: 'delivered', pending: 'waiting to send', sending: 'sending', retrying: 'will retry',
  failed: 'could not deliver', unknown: 'delivery unconfirmed', cancelled: 'cancelled', fallback: 'trying another channel',
};
const decisions: Record<string, string> = {
  EXISTING_ALERT: 'Incident updated', SUPPRESSED_COOLDOWN: 'Repeated event grouped',
  LATE_OCCURRENCE: 'Late event recorded', STALE_RESOLVE: 'Earlier recovery recorded',
  RESOLVED: 'Recovered', DIGEST_QUEUED: 'Queued for a summary', UNROUTED: 'No recipient available',
  SILENCED: 'Notifications muted',
};
const actions: Record<string, string> = {
  'alert.ack': 'Acknowledged', 'alert.resolve': 'Resolved', 'action.request': 'Requested approval',
  'action.approve': 'Approved the action', 'action.reject': 'Rejected the action',
  'approval.locked': 'Too many approval attempts',
};

function recipientName(recipient: string, directory: Directory) {
  if (recipient.startsWith('team:')) {
    const id = recipient.slice(5);
    return directory.teams.some(team => team.id === id && team.name) ? teamName(id, directory) : undefined;
  }
  return directory.users.some(user => user.id === recipient) ? personName(recipient, directory) : undefined;
}

export function timelineRows(entries: Entry[], notifications: Notification[], rules: TimelineRule[], directory: Directory) {
  const summaries = new Map<string, number>();
  const calls = new Map<string, number | undefined>();
  for (const entry of entries) {
    if (entry.kind === 'notification') summaries.set(entry.text, (summaries.get(entry.text) ?? 0) + 1);
  }
  for (const message of notifications) {
    if (message.channel !== 'phone' || message.state !== 'delivered') continue;
    const key = `phone → ${message.recipient}: delivered`;
    const latency = message.attempt_log.filter(attempt => attempt.outcome === 'delivered').at(-1)?.latency_ms;
    // Older detail responses have no notification ID per row. Only pair an unambiguous delivery.
    calls.set(key, !calls.has(key) && typeof latency === 'number' && latency >= 0 ? latency : undefined);
  }

  return [...entries].reverse().flatMap(entry => {
    let text: string;
    if (entry.kind === 'notification') {
      // Finished attempts have a trailing "(attempt N)" and are already represented by the summary.
      const match = /^(\S+) → (.+): ([a-z_]+)$/.exec(entry.text);
      if (!match) return [];
      const [, channel, recipient, state] = match;
      const name = recipientName(recipient, directory);
      const latency = summaries.get(entry.text) === 1 ? calls.get(entry.text) : undefined;
      const timing = latency === undefined ? '' : ` in ${(latency / 1000).toFixed(2)} s`;
      text = `${channels[channel] ?? 'Message'}${name ? ' to ' + name : ''} · ${delivery[state] ?? 'delivery status unavailable'}${timing}`;
    } else if (entry.kind === 'decision') {
      const [, id, outcome] = /^(.*) → (.*)$/.exec(entry.text) ?? [];
      const label = rules.find(rule => rule.id === id)?.name;
      const name = typeof label === 'string' ? label.trim() : '';
      text = outcome === 'NEW_ALERT' ? (name && name !== id ? 'Opened by rule ' + name : 'Opened by a rule')
        : (decisions[outcome] ?? 'Event evaluated');
    } else if (entry.kind === 'audit') {
      const [actor, action] = entry.text.split(' ');
      const name = recipientName(actor, directory);
      text = name && actions[action] ? `${actions[action]} by ${name}` : (actions[action] ?? 'Incident updated');
    } else return [];
    return [{...entry, text}];
  });
}
