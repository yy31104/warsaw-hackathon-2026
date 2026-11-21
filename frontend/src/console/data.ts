import {useQueries, useQuery} from '@tanstack/react-query';
import {alertListQuery, api, RequestError, request} from '../api';
import type {Alert} from '../model';
import type {Rule} from '../types';

export type Escalation = {at: string; level: string; targets: string[]; channels: string[]};
export type PendingAction = {
  run_id: number; action: string; requested_by: string; approvers: string[];
  status: string; created_at: string;
};
export type IncidentDetail = Omit<Awaited<ReturnType<typeof api.detail>>,
  'next_escalation' | 'acked_at' | 'acked_by' | 'resolved_at' | 'resolved_by'> & {
    next_escalation?: Escalation | null;
    representative_event_id?: number | null;
    pending_actions?: PendingAction[];
    acked_at: string | null;
    acked_by: string | null;
    resolved_at: string | null;
    resolved_by: string | null;
  };
export type Funnel = {
  raw: number; accepted: number; alerts_open: number; processing_backlog: number;
  backlog: number; oldest_pending_s: number; at_risk: boolean; notifications: Record<string, number>;
};
export type EditableRule = Omit<Rule, 'match' | 'group'> & {
  match: Record<string, unknown>;
  group?: NonNullable<Rule['group']> & {by?: string[]};
  expect?: {key?: string[]};
  escalate_after?: string;
};

const severityOrder = ['critical', 'error', 'warning', 'info', 'debug'];
const severityRank = (severity: string) => {
  const index = severityOrder.indexOf(severity);
  return index < 0 ? severityOrder.length : index;
};

export function orderOpenAlerts(items: Alert[]) {
  return [...items].sort((a, b) => severityRank(a.severity) - severityRank(b.severity)
    || Date.parse(a.first_seen_at) - Date.parse(b.first_seen_at) || a.id - b.id);
}

export function canReview(action: PendingAction, actor: string) {
  return action.requested_by !== actor && action.approvers.includes(actor);
}

export function representativeExplainPath(incident: {representative_event_id?: number | null}) {
  const id = incident.representative_event_id;
  return typeof id === 'number' && Number.isSafeInteger(id) && id > 0 ? `/events/${id}/explain` : null;
}

export function detailQuery(id: number | undefined) {
  return {
    queryKey: ['alerts', 'detail', id],
    queryFn: (): Promise<IncidentDetail> => api.detail(id!),
    enabled: id !== undefined,
  };
}

export function useConsoleQueue(team: string, enabled: boolean) {
  const open = useQuery(alertListQuery(team, enabled, 'open'));
  const pending = useQuery({
    queryKey: ['pending-approvals', team],
    queryFn: () => request<{pending_approval_count: number}>(`/stats/pending-approvals?team=${encodeURIComponent(team)}`),
    enabled,
  });
  const loadPending = enabled && (pending.data?.pending_approval_count ?? 0) > 0;
  const list = useQuery(alertListQuery(team, loadPending));
  const details = useQueries({
    queries: loadPending ? (list.data ?? []).map(alert => detailQuery(alert.id)) : [],
  });
  const actions = details.flatMap(result => {
    const alert = result.data;
    return alert?.pending_actions?.map(action => ({alert, action})) ?? [];
  });
  return {open, pending, actions};
}

export function useFunnel() {
  return useQuery({
    queryKey: ['console-funnel'],
    queryFn: () => request<Funnel>('/stats/funnel?window=24h'),
    refetchInterval: 10000,
  });
}

type ResponseMetric = 'tta_p50_s' | 'mtta_s' | 'mttr_s' | 'notifications_per_alert';

type ResponseStats = Record<ResponseMetric, number | null> & {
  n_alerts: number;
  sample_counts?: Partial<Record<ResponseMetric, number>>;
};

export function useResponseStats(team: string, enabled: boolean) {
  return useQuery({
    queryKey: ['console-response', team],
    queryFn: async () => {
      try {
        return await request<ResponseStats>(`/stats/response?window=24h&team=${encodeURIComponent(team)}`);
      } catch (error) {
        if (error instanceof RequestError && error.status === 404) return null;
        throw error;
      }
    },
    enabled,
    retry: false,
    refetchInterval: query => query.state.data === null ? false : 10000,
  });
}

export type Problem = {code?: string; message: string; path?: string; fix_example?: unknown; suggestions?: string[]};

export function requestProblems(error: unknown): Problem[] {
  if (!(error instanceof RequestError)) return [];
  const detail = error.detail;
  if (Array.isArray(detail)) {
    return detail.map(item => ({
      code: item.code ?? item.type,
      message: item.message ?? item.msg,
      path: item.path ?? item.loc?.join('.'),
      fix_example: item.fix_example,
      suggestions: item.suggestions,
    })).filter(problem => typeof problem.message === 'string');
  }
  if (detail && typeof detail === 'object' && 'problems' in detail && Array.isArray(detail.problems)) {
    return detail.problems;
  }
  return [];
}

function replaceUnknownPath(match: unknown, from: string, to: string): unknown {
  if (!match || typeof match !== 'object' || Array.isArray(match)) return match;
  return Object.fromEntries(Object.entries(match).map(([key, value]) => {
    if (key === 'field' && value === from) return [key, to];
    if (key === 'all' || key === 'any') {
      return [key, Array.isArray(value) ? value.map(child => replaceUnknownPath(child, from, to)) : value];
    }
    if (key === 'not') return [key, replaceUnknownPath(value, from, to)];
    return [key === from ? to : key, value];
  }));
}

export function applyRulePathSuggestion(rule: EditableRule, from: string, to: string): EditableRule {
  const paths = (values?: string[]) => values?.map(path => path === from ? to : path);
  const template = (value?: string) => value?.replace(/\{\{\s*([^{}]+?)\s*\}\}/g,
    (token, path: string) => path === from ? '{{' + to + '}}' : token);
  return {
    ...rule,
    match: replaceUnknownPath(rule.match, from, to) as EditableRule['match'],
    present: rule.present && {...rule.present, title: template(rule.present.title),
      body: template(rule.present.body), fields: paths(rule.present.fields)},
    group: rule.group && {...rule.group, by: paths(rule.group.by)},
    expect: rule.expect && {...rule.expect, key: paths(rule.expect.key)},
  };
}
