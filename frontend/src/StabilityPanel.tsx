import {useState} from 'react';
import {request} from './api';
import {Card, channelName, Chip, Failure} from './console/ui';

type Fields = Record<string, unknown>;
type Draft = Fields & {id: string; name: string; aliases?: Record<string, string>};
type Suggestion = {draft: Draft; rationale?: Record<string, string>};
type Candidate = {value: string | number; source: string; label?: string};
type Hypothesis = {id: string; kind: string; path: string; label?: string; candidates: Candidate[]};
type Question = {outcome_group: string} | {question_id: string; branches: {option_index: number; next: Question}[]};
type Outcome = {
  sample_index: number;
  matched_rule_ids: string[];
  alerts: {rule_id: string; severity: string; priority: string | null; alert_key: string}[];
  deliveries: {recipient: string; channel: string}[];
};
type Group = {id: string; outcome: Outcome[]; hypotheses: Record<string, number>[]};
type Result = {
  complete: boolean;
  decision_invariant: boolean | null;
  evaluated_combinations: number;
  total_combinations: number;
  relevant_unknowns: string[];
  ordered_questions: Hypothesis[];
  hypotheses: Hypothesis[];
  question_tree: Question | null;
  outcome_groups: Group[];
  warnings: string[];
  snapshot: {id: string; revision: string | number | null};
};

function priorAliases(draft: Draft, suggestion?: Suggestion) {
  if (!suggestion) return [];
  return Object.entries(draft.aliases ?? {}).map(([path, value]) => {
    const source = suggestion.draft.aliases?.[path] !== value ? 'declared'
      : suggestion.rationale?.[path]?.startsWith('Model proposed ') ? 'llm_suggestion' : 'heuristic';
    return {kind: 'alias', path, candidates: [{value, source}]};
  });
}

function readSample(text: string): Fields {
  const parsed: unknown = JSON.parse(text);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Enter a JSON object for each sample.');
  return parsed as Fields;
}

function hypothesisName(hypothesis: Hypothesis) {
  return hypothesis.kind === 'severity' && hypothesis.label
    ? `Severity for ${hypothesis.label}`
    : hypothesis.label ?? hypothesis.path;
}

export function StabilityPanel({draft, positive, quiet, source, transport, actor, suggestion}: {
  draft: Draft; positive: string; quiet: string; source: string; transport: string; actor: string;
  suggestion?: Suggestion;
}) {
  const [result, setResult] = useState<Result>();
  const [answers, setAnswers] = useState<Record<string, number>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function check() {
    setBusy(true);
    setError(null);
    setResult(undefined);
    setAnswers({});
    try {
      const samples = [positive, quiet].filter(text => text.trim()).map(text => ({event: readSample(text), source, transport}));
      const previewDraft = {...draft, name: draft.name.trim() || suggestion?.draft.name || draft.id};
      setResult(await request<Result>('/schemas/stability', actor, {
        draft: previewDraft, samples, hypotheses: priorAliases(draft, suggestion),
      }));
    } catch (problem) {
      setError(problem);
    } finally {
      setBusy(false);
    }
  }

  const byId = new Map(result?.hypotheses.map(item => [item.id, item]));
  const relevant = new Set(result?.relevant_unknowns);
  const unknowns = (result?.hypotheses ?? []).filter(item => item.candidates.length > 1);
  const groups = (result?.outcome_groups ?? []).map(group => ({...group, hypotheses: group.hypotheses.filter(world =>
    Object.entries(answers).every(([id, option]) => world[id] === option))})).filter(group => group.hypotheses.length);
  const questions: {hypothesis: Hypothesis; value?: number; previous: Record<string, number>}[] = [];
  let node = result?.question_tree;
  let previous: Record<string, number> = {};
  while (node && 'question_id' in node) {
    const hypothesis = byId.get(node.question_id);
    if (!hypothesis) break;
    const value = answers[hypothesis.id];
    questions.push({hypothesis, value, previous});
    previous = {...previous, [hypothesis.id]: value};
    node = node.branches.find(branch => branch.option_index === value)?.next;
  }

  const critical = result?.warnings.includes('may page critical: keep in review');
  const determining = (world: Record<string, number>) => Object.entries(world)
    .filter(([id]) => relevant.has(id)).map(([id, index]) => {
      const hypothesis = byId.get(id);
      const option = hypothesis?.candidates[index];
      return `${hypothesis ? hypothesisName(hypothesis) : id} → ${option?.label ?? option?.value ?? index}`;
    }).join(' · ');

  return <Card title="Decision stability" meta={result?.complete
    ? `${unknowns.length} uncertainties · ${relevant.size} affect routing`
    : result ? `${result.evaluated_combinations} / ${result.total_combinations} combinations checked` : undefined}>
    <button className="cf-btn" disabled={busy || !positive.trim()} onClick={() => void check()}>
      {busy ? 'Checking interpretations…' : 'Compare possible interpretations'}
    </button>
    <Failure error={error}/>
    {result && !result.complete && <p role="alert"><Chip tone="amber">INCOMPLETE</Chip> Review the mapping before drawing a routing conclusion.</p>}
    {critical && <p role="alert"><Chip tone="red">Critical possible</Chip> May page critical. Keep in review.</p>}
    {result?.complete && <div className="cf-stack">
      <p>{result.decision_invariant ? 'The tested interpretations produce the same decision.' :
        groups.length === 1 ? 'The answers determine the decision for these samples.' : 'Different interpretations change the decision.'}
        {' '}Answers here are for preview. Confirm the producer’s semantics in the mapping before publishing.</p>
      {questions.map(({hypothesis, value, previous: earlier}) => <label className="cf-field" key={hypothesis.id}>
        {hypothesisName(hypothesis)}
        <select value={value ?? ''} onChange={event => {
          setAnswers(event.target.value === '' ? earlier : {...earlier, [hypothesis.id]: Number(event.target.value)});
        }}>
          <option value="">Choose an interpretation</option>
          {hypothesis.candidates.map((candidate, index) => <option value={index} key={index}>
            {candidate.label ?? String(candidate.value)} · {candidate.source.replaceAll('_', ' ')}
          </option>)}
        </select>
      </label>)}
      <div className="cf-grid">
        {groups.map(group => <section key={group.id} className="cf-sample-box">
          {group.outcome.map(outcome => <div key={outcome.sample_index}>
            {group.outcome.length > 1 && <small>Sample {outcome.sample_index + 1}</small>}
            <p><strong>{outcome.deliveries.length
              ? outcome.deliveries.map(({recipient, channel}) => `${recipient} · ${channelName(channel)}`).join(', ')
              : outcome.matched_rule_ids.length ? 'No immediate delivery' : 'Quiet'}</strong></p>
            {outcome.matched_rule_ids.length > 0 && <p><small>Matched {outcome.matched_rule_ids.join(', ')}</small></p>}
            {outcome.alerts.map(alert => <small key={alert.rule_id + alert.alert_key}>
              {alert.rule_id} · {alert.severity} {alert.priority} · {alert.alert_key}
            </small>)}
          </div>)}
          {relevant.size > 0 && <ul>
            {[...new Set(group.hypotheses.map(determining))].map(condition => <li key={condition}>If {condition}</li>)}
          </ul>}
        </section>)}
      </div>
      {unknowns.filter(item => !relevant.has(item.id)).map(item => <small style={{opacity: .68}} key={item.id}>
        {hypothesisName(item)} doesn’t affect routing under current rules (still unconfirmed).
      </small>)}
    </div>}
  </Card>;
}
