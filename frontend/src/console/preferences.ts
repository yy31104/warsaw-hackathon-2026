import type {Preference} from '../types';

const minutesPerDay = 24 * 60;

export function toMinutes(value: string) {
  const [hours, minutes] = value.split(':').map(Number);
  return hours * 60 + minutes;
}

export function hhmm(minute: number) {
  return [Math.floor(minute / 60), minute % 60].map(value => String(value).padStart(2, '0')).join(':');
}

export function channelsAt(rows: Preference[], minute: number) {
  const matched = rows.find(row => {
    if (!row.when) return true;
    const [start, end] = row.when.split('-').map(toMinutes);
    return start <= end ? start <= minute && minute < end : minute >= start || minute < end;
  });
  return matched?.channels ?? [];
}

export function daySegments(rows: Preference[]) {
  const cuts = [...new Set([0, minutesPerDay, ...rows.flatMap(row => row.when ? row.when.split('-').map(toMinutes) : [])])]
    .sort((a, b) => a - b);
  return cuts.slice(0, -1).map((start, index) => ({start, end: cuts[index + 1], channels: channelsAt(rows, start)}));
}
