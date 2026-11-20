import {useState} from 'react';
import {Badge, Button, Select, Textarea, TextInput} from '@mantine/core';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {ErrorBox, PageTitle, Panel, useApp} from './App';
import {request} from './api';
import {ownsRule} from './permissions';

type Runbook = {
  id: string;
  content: string;
  format: string;
  rules?: string[];
  matches?: Record<string, unknown>;
  owner?: string;
  origin: 'file' | 'ui';
  version: number;
};
type Structured = {source: string; draft: {format: string; content: string}; warnings: string[]};

export function RunbookCatalogue() {
  const {actor, directory, notice} = useApp();
  const client = useQueryClient();
  const books = useQuery({queryKey: ['runbooks'], queryFn: () => request<{items: Runbook[]}>('/runbooks')});
  const [book, setBook] = useState<Runbook | null>(null);
  const [id, setId] = useState('');
  const [content, setContent] = useState('');
  const [format, setFormat] = useState<string | null>(null);
  const [rules, setRules] = useState('');
  const [structure, setStructure] = useState<Structured | null>(null);
  const [draftSource, setDraftSource] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const readOnly = book?.origin === 'file' || (book?.owner && !ownsRule(book.owner, actor, false, directory));

  function edit(selected: Runbook | null) {
    setBook(selected);
    setId(selected?.id ?? '');
    setContent(selected?.content ?? '');
    setFormat(selected?.format ?? null);
    setRules(selected?.rules?.join(', ') ?? '');
    setStructure(null);
    setDraftSource(undefined);
    setError('');
  }

  async function perform(task: () => Promise<void>) {
    setBusy(true);
    setError('');
    try { await task(); }
    catch (error) { setError(String(error)); }
    finally { setBusy(false); }
  }

  async function structureRunbook() {
    const result = await request<Structured>('/runbooks/structure', actor, {text: content});
    setStructure(result);
  }

  async function save() {
    const saved = await request<Runbook>('/runbooks', actor, {
      id,
      format,
      content,
      rules: rules.split(',').map(rule => rule.trim()).filter(Boolean),
      owner: book?.owner ?? `user:${actor}`,
      version: book?.version,
      matches: book?.matches,
      source: draftSource,
    });
    edit(saved);
    void client.invalidateQueries({queryKey: ['runbooks']});
    notice(`Runbook ${saved.id} v${saved.version} saved`);
  }

  return <>
    <PageTitle title="Runbooks" description="Store runbook instructions and optionally review a suggested set of steps."/>
    <ErrorBox error={error || books.error}/>
    <Panel title="Configured runbooks">
      {books.data?.items.map(item => <div className="row" key={item.id}>
        <div><strong>{item.id}</strong> · {item.format} · v{item.version} <Badge>{item.origin}</Badge></div>
        <Button variant="subtle" disabled={busy} onClick={() => edit(item)}>Open</Button>
      </div>)}
      {books.isPending && <p>Loading runbooks…</p>}
      {books.data?.items.length === 0 && <p>No runbooks configured.</p>}
      <Button variant="default" disabled={busy} onClick={() => edit(null)}>New runbook</Button>
    </Panel>
    <Panel title={book ? `Runbook ${book.id}` : 'New runbook'}>
      {readOnly && <p>{book?.origin === 'file' ? 'This runbook is managed by a configuration file.' : 'Editing requires ownership of this runbook.'}</p>}
      <div className="form-grid">
        <TextInput label="Runbook ID" value={id} disabled={busy || !!book} onChange={event => setId(event.target.value)}/>
        <Select label="Format" placeholder="Auto-detect" clearable data={['yaml-steps', 'markdown', 'checklist', 'text', 'url']}
          value={format} disabled={busy || !!readOnly} onChange={setFormat}/>
      </div>
      <TextInput label="Rule IDs (optional)" value={rules} readOnly={busy || !!readOnly} onChange={event => setRules(event.target.value)}/>
      <Textarea label="Runbook content" autosize minRows={8} maxRows={24} value={content} readOnly={busy || !!readOnly}
        onChange={event => {setContent(event.target.value); setStructure(null);}}/>
      <div className="actions">
        <Button variant="default" disabled={busy || !!readOnly || !content.trim()} onClick={() => void perform(structureRunbook)}>Structure with AI</Button>
        <Button disabled={busy || !!readOnly || !id.trim() || !content.trim()} onClick={() => void perform(save)}>Save runbook</Button>
      </div>
      {structure && <>
        <h3>Suggested runbook steps</h3>
        <Badge>Draft source · {structure.source}</Badge>
        {structure.warnings.map(warning => <p key={warning}>{warning}</p>)}
        <pre>{structure.draft.content}</pre>
        <Button variant="default" onClick={() => {
          setContent(structure.draft.content);
          setFormat(structure.draft.format);
          setDraftSource(structure.source);
          setStructure(null);
        }}>Use draft in editor</Button>
      </>}
    </Panel>
  </>;
}
