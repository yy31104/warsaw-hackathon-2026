import {parse} from 'yaml';
import type {Problem} from './data';

export type StepDraft = {
  text?: string; check?: string; action?: string; approval?: 'none' | 'required';
  problems?: Problem[]; [key: string]: unknown;
};

const requiresApproval = new Set(['restart_service', 'escalate', 'call_webhook']);

export function readSteps(content: string): {steps: StepDraft[]; problems: Problem[]} {
  let document: unknown;
  try {
    document = parse(content);
  } catch {
    return {steps: [], problems: [{path: 'steps', message: 'Enter valid YAML.', fix_example: 'steps:\n  - text: Check the dashboard'}]};
  }
  if (!document || typeof document !== 'object' || !('steps' in document) || !Array.isArray(document.steps)
    || document.steps.some(step => !step || typeof step !== 'object' || Array.isArray(step))) {
    return {steps: [], problems: [{path: 'steps', message: 'Add a YAML list of steps.', fix_example: 'steps:\n  - text: Check the dashboard'}]};
  }
  const steps = document.steps as StepDraft[];
  const problems: Problem[] = [];
  steps.forEach((step, index) => {
    const path = `steps.${index}`;
    if (step.action != null && typeof step.action !== 'string') {
      problems.push({path: path + '.action', message: 'Use an action name.'});
    }
    if (step.approval !== undefined && !['none', 'required'].includes(step.approval)) {
      problems.push({path: path + '.approval', message: 'Use none or required.', fix_example: 'approval: required'});
    } else if (step.action && step.approval === 'none' && requiresApproval.has(step.action)) {
      problems.push({path: path + '.approval', message: 'This action requires approval.', fix_example: 'approval: required'});
    }
  });
  return {steps, problems};
}

export function problemIndex(problem: Problem) {
  const match = /(?:^|\.)steps(?:\.|\[)(\d+)/.exec(problem.path ?? '');
  return match ? Number(match[1]) : null;
}

export function draftStepKind(step: StepDraft) {
  if (step.approval === 'required' || (step.action && requiresApproval.has(step.action))) return 'Needs approval';
  if (step.action === 'http_get' || step.check) return 'Read-only';
  return step.action ? 'Action' : 'Manual';
}
