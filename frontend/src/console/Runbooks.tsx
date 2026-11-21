import {useState} from 'react';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {useApp} from '../App';
import {request, RequestError} from '../api';
import {ownsRule} from '../permissions';
import {requestProblems, type Problem} from './data';
import {draftStepKind, problemIndex, readSteps} from './runbookDraft';
import {Card, Chip, Failure, humanize, Icon, LegacyLink, Problems} from './ui';

type Book = {
  id: string; content: string; format: string; owner?: string; origin: 'file' | 'ui';
  version: number; rules?: string[]; matches?: Record<string, unknown>;
};
type Structure = {
  draft: {format: string; content: string; steps?: {problems?: Problem[]}[]};
  source: 'heuristic' | 'llm'; model?: string; warnings?: string[]; problems?: Problem[];
};
const formats = ['yaml-steps', 'markdown', 'checklist', 'text', 'url'];

function Editor({book, onSaved}: {book: Book | null; onSaved: (book: Book) => void}) {
  const {actor, directory, notice} = useApp();
  const [id, setId] = useState(book?.id ?? '');
  const [format, setFormat] = useState(book?.format ?? '');
  const [original, setOriginal] = useState(book?.content ?? '');
  const [structured, setStructured] = useState<string>();
  const [structure, setStructure] = useState<Structure>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const readOnly = book?.origin === 'file' || Boolean(book?.owner && !ownsRule(book.owner, actor, false, directory));
  const content = structured ?? original;
  const actualFormat = structured != null || (!format && /^\s*steps\s*:/m.test(content)) ? 'yaml-steps' : format;
  const decoded = actualFormat === 'yaml-steps' && content.trim() ? readSteps(content) : {steps: [], problems: []};
  const problems = [...decoded.problems, ...requestProblems(error), ...(structure?.problems ?? []),
    ...(structure?.draft.steps ?? []).flatMap((step, index) => (step.problems ?? []).map(problem => ({
      ...problem, path: problem.path && problemIndex(problem) != null ? problem.path : `steps.${index}`,
    })))];
  const templates = [...new Set(content.match(/\{\{\s*[\w.$-]+\s*\}\}/g) ?? [])];

  async function perform(task: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try { await task(); } catch (problem) { setError(problem); } finally { setBusy(false); }
  }

  async function convert() {
    const result = await request<Structure>('/runbooks/structure', actor, {text: original});
    setStructured(result.draft.content);
    setStructure(result);
  }

  async function publish() {
    const saved = await request<Book>('/runbooks', actor, {
      id, format: actualFormat || undefined, content,
      owner: book?.owner ?? `user:${actor}`,
      version: book?.version,
      matches: book?.matches,
      rules: book?.rules,
      source: structure?.source,
    });
    onSaved(saved);
    notice(`Published ${saved.id} · v${saved.version}`);
  }

  return <div className="cf-stack">
    {Boolean(error) && requestProblems(error).length === 0 && <Failure error={error}/>}
    <div className="cf-grid">
      <Card title="As written" meta={readOnly ? 'File managed' : book ? `v${book.version}` : undefined}>
        <div className="cf-form-row">
          <label className="cf-field">Runbook ID<input value={id} disabled={Boolean(book) || busy} onChange={event => {
            setId(event.target.value); setError(null);
          }}/></label>
          <label className="cf-field">Format<select value={actualFormat} disabled={readOnly || busy} onChange={event => {
            setFormat(event.target.value);
            setStructured(undefined);
            setStructure(undefined);
            setError(null);
          }}><option value="">Auto-detect</option>{formats.map(item => <option value={item} key={item}>{humanize(item)}</option>)}</select></label>
        </div>
        <label className="cf-field">Original text<textarea aria-label="Original text" className="cf-code-input" rows={12} value={original}
          readOnly={readOnly || busy} spellCheck={false} onChange={event => {
            setOriginal(event.target.value); setStructured(undefined); setStructure(undefined); setError(null);
          }}/></label>
        {!readOnly && <button className="cf-btn" disabled={busy || !original.trim()} onClick={() => void perform(convert)}>Turn into steps</button>}
      </Card>
      <Card title="As steps" meta={structure ? [structure.source === 'llm' ? 'AI draft' : 'Rules draft', structure.model]
        .filter(Boolean).join(' · ') : undefined}>
        <Problems items={problems.filter(problem => problemIndex(problem) == null || !decoded.steps[problemIndex(problem)!])}/>
        {decoded.steps.map((step, index) => {
          const errors = problems.filter(problem => problemIndex(problem) === index);
          const kind = draftStepKind(step);
          return <div className="cf-step" key={index}>
            <div className={`cf-step-icon cf-${errors.length ? 'red' : kind === 'Needs approval' ? 'amber' : 'neutral'}`}>
              <Icon name={kind === 'Needs approval' ? 'shield' : kind === 'Read-only' ? 'check' : 'runbooks'}/>
            </div>
            <div className="cf-step-copy"><strong>{step.text || step.check || humanize(typeof step.action === 'string' ? step.action : 'Manual step')}</strong>
              <small>{kind}</small><Problems items={errors}/>
            </div>
          </div>;
        })}
        {!decoded.steps.length && !problems.length && <p>{actualFormat === 'yaml-steps' ? 'Add a step.' : 'Structure text to review steps.'}</p>}
        {structured !== undefined && <details>
          <summary>Edit steps</summary>
          <label className="cf-field">Steps YAML<textarea aria-label="Steps YAML" rows={10} value={structured} readOnly={busy || readOnly}
            spellCheck={false} onChange={event => {
              setStructured(event.target.value); setStructure(undefined); setError(null);
            }}/></label>
        </details>}
        {!!templates.length && <div className="cf-chips">{templates.map(template => <Chip key={template}>
          <code>{template}</code>{/\bworkflowId\b/.test(template) && <small>e.g. wf-8812</small>}
        </Chip>)}</div>}
        {structure?.warnings?.slice(0, 2).map(warning => <small className="cf-meta cf-block" key={warning}>{warning}</small>)}
      </Card>
    </div>
    {!readOnly && <div className="cf-actions">
      <button className="cf-btn primary" disabled={busy || !id.trim() || !content.trim()
        || problems.length > 0 || (error instanceof RequestError && error.status === 422)}
        onClick={() => void perform(publish)}>Publish</button>
      {problems.length > 0 && <Chip tone="red">{problems.length} to fix</Chip>}
    </div>}
  </div>;
}

export function ConsoleRunbooks() {
  const client = useQueryClient();
  const books = useQuery({queryKey: ['runbooks'], queryFn: () => request<{items: Book[]}>('/runbooks')});
  const [selection, setSelection] = useState<string>();
  const [published, setPublished] = useState<Book>();
  const items = books.data?.items ?? [];
  const book = selection === '' ? null : published?.id === selection && published ? published
    : items.find(item => item.id === selection) ?? items[0] ?? null;
  return <div className="cf-stack">
    <Failure error={books.error}/>
    <div className="cf-actions">
      <label className="cf-field cf-picker">Runbook<select value={book?.id ?? ''} onChange={event => setSelection(event.target.value)}>
        <option value="">New runbook</option>
        {published && !items.some(item => item.id === published.id) && <option value={published.id}>{published.id}</option>}
        {items.map(item => <option value={item.id} key={item.id}>{item.id}</option>)}
      </select></label>
      <button className="cf-btn" onClick={() => setSelection('')}>New runbook</button>
      <LegacyLink to="/runbooks/catalogue">Catalogue</LegacyLink>
    </div>
    {books.isPending && selection !== '' ? <p>Loading runbooks…</p> : <Editor
      key={book ? `${book.id}:${book.version}` : 'new'} book={book} onSaved={saved => {
        setPublished(saved);
        setSelection(saved.id);
        void client.invalidateQueries({queryKey: ['runbooks']});
      }}/>}
  </div>;
}
