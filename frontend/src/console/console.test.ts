import {describe, expect, it} from 'vitest';
import {applyRulePathSuggestion, canReview, orderOpenAlerts, representativeExplainPath} from './data';
import {channelsAt, daySegments, toMinutes} from './preferences';
import {readSteps} from './runbookDraft';
import alerts from '../../../fixtures/api/alerts.json';

describe('console decisions', () => {
  it('orders the hero by severity and then by earliest opening time', () => {
    const seed = alerts.items[0];
    const later = {...seed, id: 21, severity: 'critical', first_seen_at: '2026-11-20T08:03:00Z'};
    const earlier = {...later, id: 22, first_seen_at: '2026-11-20T08:02:00Z'};
    const lower = {...earlier, id: 23, severity: 'error', first_seen_at: '2026-11-20T08:00:00Z'};
    expect(orderOpenAlerts([lower, later, earlier]).map(alert => alert.id)).toEqual([22, 21, 23]);
  });

  it('requests explain using only the representative event ID', () => {
    const alert = {id: 42, occurrences: [{event_id: 65}], representative_event_id: 93};
    expect(representativeExplainPath(alert)).toBe('/events/93/explain');
    expect(representativeExplainPath({...alert, representative_event_id: undefined})).toBeNull();
    expect(representativeExplainPath({...alert, representative_event_id: null})).toBeNull();
  });

  it('only offers approval to an eligible second person', () => {
    const action = {run_id: 1, action: 'retry_workflow', requested_by: 'sam', approvers: ['sam', 'lee'],
      status: 'pending_approval', created_at: '2026-11-20T08:00:00Z'};
    expect(canReview(action, 'lee')).toBe(true);
    expect(canReview(action, 'sam')).toBe(false);
    expect(canReview(action, 'rita')).toBe(false);
  });

  it('applies path suggestions inside nested conditions without replacing values', () => {
    const match = {all: [{field: 'errorCode', op: 'eq', value: 'errorCode'},
      {any: [{not: {errorCode: 'IGNORE'}}, {severity: 'error'}]}]};
    const changed = applyRulePathSuggestion({id: 'rule', origin: 'ui', owner: 'user:lee', version: 1, enabled: true,
      route: [], match, present: {title: '{{ errorCode }}', body: 'Literal errorCode', fields: ['errorCode']},
      group: {mode: 'immediate', by: ['errorCode']}, expect: {key: ['errorCode']}}, 'errorCode', 'error.code');
    expect(changed.match).toEqual({all: [
      {field: 'error.code', op: 'eq', value: 'errorCode'}, {any: [{not: {'error.code': 'IGNORE'}}, {severity: 'error'}]},
    ]});
    expect(changed.present).toEqual({title: '{{error.code}}', body: 'Literal errorCode', fields: ['error.code']});
    expect(changed.group?.by).toEqual(['error.code']);
    expect(changed.expect?.key).toEqual(['error.code']);
  });

  it('shows ordered windows across midnight and keeps the end exclusive', () => {
    const rows = [
      {when: '22:30-06:45', min_severity: 'critical', channels: ['sms']},
      {min_severity: 'critical', channels: ['email']},
    ];
    expect(channelsAt(rows, toMinutes('22:30'))).toEqual(['sms']);
    expect(channelsAt(rows, toMinutes('06:44'))).toEqual(['sms']);
    expect(channelsAt(rows, toMinutes('06:45'))).toEqual(['email']);
    expect(daySegments(rows).map(slot => slot.channels)).toEqual([['sms'], ['email'], ['sms']]);
  });

  it('marks the saved runbook step that needs approval', () => {
    const broken = 'steps:\n  - action: restart_service\n    service: sample\n    approval: none';
    const result = readSteps(broken);
    expect(result.problems).toMatchObject([{path: 'steps.0.approval', fix_example: 'approval: required'}]);
    expect(readSteps(broken.replace('approval: none', 'approval: required')).problems).toEqual([]);
  });
});
