import {useState} from 'react';
import {Button,Badge,Textarea} from '@mantine/core';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import YAML from 'yaml';
import {Panel,PageTitle,useApp,ErrorBox} from './App';
import {store,persist} from './store';
import {record,useSession} from './session';
import {ownsRule} from './permissions';
import {MOCK_MODE, request} from './api';
import type {Rule} from './types';
import {ImpactResults} from './VerificationPanels';
import {ImpactPreview, type ImpactPreviewResult} from './ImpactPreview';

type Proposal = {
  id: number;
  alert_id: number;
  rule_id: string;
  base_version: number;
  text: string;
  status: string;
  proposed_by: string;
  draft: Partial<Rule>;
};

function LiveProposalPage() {
  const {actor, directory, notice} = useApp();
  const client = useQueryClient();
  const proposals = useQuery({queryKey: ['proposals', actor], queryFn: () => request<{items: Proposal[]}>('/proposals', actor)});
  const rules = useQuery({queryKey: ['rules'], queryFn: async () => (await request<{items: Rule[]}>('/rules')).items});
  return <>
    <PageTitle title="Rule proposals" description="Review the requested change and replay it on stored events before approving."/>
    <ErrorBox error={proposals.error || rules.error}/>
    <Panel title="Change requests">
      {proposals.isPending && <p>Loading proposals…</p>}
      {proposals.data?.items.length === 0 && <p>No proposals yet. Propose a change from the incident.</p>}
      {proposals.data?.items.map(proposal => <LiveProposal key={`${actor}:${proposal.id}`} proposal={proposal}
        rule={rules.data?.find(rule => rule.id === proposal.rule_id)} mayApprove={rules.data?.some(rule =>
          rule.id === proposal.rule_id && rule.origin === 'ui' && ownsRule(rule.owner, actor, false, directory)) ?? false}
        onApproved={async saved => {
          await client.invalidateQueries({queryKey: ['proposals']});
          await client.invalidateQueries({queryKey: ['rules']});
          notice(`Proposal approved · ${saved.id} v${saved.version}`);
        }}/>) }
    </Panel>
  </>;
}

function LiveProposal({proposal, rule, mayApprove, onApproved}: {
  proposal: Proposal; rule?: Rule; mayApprove: boolean; onApproved: (rule: Rule) => Promise<void>;
}) {
  const {actor} = useApp();
  const [yaml, setYaml] = useState<string | null>(null);
  const [result, setResult] = useState<{yaml: string | null; result: ImpactPreviewResult} | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const stale = rule?.version !== proposal.base_version;

  async function submit(action: 'preview' | 'approve') {
    setBusy(true);
    setError('');
    try {
      const draft = YAML.parse(yaml ?? '') as Rule;
      if (draft?.id !== proposal.rule_id || !draft.match || !draft.route?.length) throw new Error('Provide conditions and recipients for the proposal’s existing rule ID.');
      if (action === 'preview') {
        setResult(null);
        setResult({yaml, result: await request<ImpactPreviewResult>(`/rules/${encodeURIComponent(proposal.rule_id)}/preview`, actor, {draft, last_n: 500})});
      } else {
        const result = await request<{saved_rule: Rule}>(`/proposals/${proposal.id}/approve`, actor, {draft});
        await onApproved(result.saved_rule);
      }
    } catch (error) { setError(String(error)); }
    finally { setBusy(false); }
  }

  return <div className="step">
    <Badge>{proposal.status}</Badge>
    <h3>{proposal.text}</h3>
    <p>{proposal.rule_id} · Rule v{proposal.base_version} · Incident #{proposal.alert_id} · Requested by {proposal.proposed_by}</p>
    {rule && proposal.status === 'open' && <Button variant="default" onClick={() => {
      setYaml(YAML.stringify(Object.keys(proposal.draft).length ? proposal.draft : rule));
      setResult(null);
    }}>Review draft and impact</Button>}
    {yaml !== null && proposal.status === 'open' && <>
      <Textarea label="Proposed rule YAML" autosize minRows={9} maxRows={24} value={yaml} onChange={event => {
        setYaml(event.target.value);
        setResult(null);
      }}/>
      <div className="actions">
        <Button variant="default" disabled={busy} onClick={() => void submit('preview')}>Preview proposal</Button>
        <Button disabled={busy || !mayApprove || stale} onClick={() => void submit('approve')}>Approve and save rule</Button>
      </div>
      {rule?.origin === 'file' ? <p>This rule is managed by a configuration file.</p>
        : stale ? <p>The rule has changed since this proposal was submitted.</p>
        : !mayApprove && <p>Rule ownership is required to approve this proposal.</p>}
      <ErrorBox error={error}/>
      {result && result.yaml === yaml && <ImpactPreview result={result.result}/>}
    </>}
  </div>;
}

export function ProposalPage() { return MOCK_MODE ? <MockProposalPage/> : <LiveProposalPage/>; }
function MockProposalPage(){const{actor,admin,notice}=useApp();useSession();const[selected,setSelected]=useState<number|null>(null),[yaml,setYaml]=useState(''),[draft,setDraft]=useState<Rule|null>(null),[error,setError]=useState('');return <><PageTitle title="Rule proposals" description="Review a concrete rule change and its sample impact before approving. Runbook approvals are separate."/><ErrorBox error={error}/><Panel title="Change requests">{!store.proposals.length?<p>No proposals yet. Open an incident and propose a rule change.</p>:store.proposals.map(p=><div className="step" key={p.id}><Badge color={p.state==='pending'?'orange':'teal'}>{p.state}</Badge><h3>{p.reason}</h3><p>Rule: {p.rule.id} · Requester: {p.actor} · Owner: {p.rule.owner}</p><Button variant="default" onClick={()=>{setSelected(p.id);const copy={...p.rule,id:p.rule.origin==='file'?`${p.rule.id}-proposal-${p.id}`:p.rule.id,origin:'ui' as const};setDraft(copy);setYaml(YAML.stringify(copy));}}>Review draft and impact</Button>{selected===p.id&&draft&&<><p>File rules are immutable. Approval of a file-rule proposal creates a separate UI-owned rule; it does not disable the original. Review possible duplicate notifications before using this copy.</p><Textarea label="Proposed rule YAML" minRows={12} value={yaml} readOnly={!ownsRule(p.rule.owner,actor,admin)||p.state!=='pending'} onChange={e=>setYaml(e.target.value)}/><Button variant="default" onClick={()=>{try{const parsed=YAML.parse(yaml) as Rule;if(!parsed.id||!parsed.match||!parsed.route)throw new Error('Draft needs id, match and route');setDraft(parsed);setError('');}catch(e){setError(String(e));}}}>Update preview from draft</Button><ImpactResults current={p.rule} draft={draft} sample={JSON.stringify({'$schema':'WORKFLOW_FAILED',workflowName:'order-settlement',environment:'prod','$severity':'error'})}/><Button disabled={p.state!=='pending'||!ownsRule(p.rule.owner,actor,admin)} onClick={()=>{try{const parsed=YAML.parse(yaml) as Rule;if(!parsed?.id||!parsed.match||!Array.isArray(parsed.route)||!parsed.route.length)throw new Error('Invalid proposed rule');const existing=store.rules.find(r=>r.id===parsed.id);if(existing?.origin==='file')throw new Error('Cannot overwrite a file rule');if(existing&&!ownsRule(existing.owner,actor,admin))throw new Error('Cannot overwrite another owner’s rule');const approved={...parsed,owner:p.rule.owner,origin:'ui' as const,version:(existing?.version??0)+1};store.rules=store.rules.filter(r=>r.id!==approved.id).concat(approved);p.rule=approved;p.state='approved';persist();record(`${actor} approved proposal and saved ${approved.id} v${approved.version}`,'config');notice('Mock proposal approved and rule saved');}catch(e){setError(String(e));}}}>Approve and save mock rule</Button>{!ownsRule(p.rule.owner,actor,admin)&&<p>Switch to the rule owner/team approver, or Admin simulation, to approve.</p>}</>}</div>)}</Panel></>;}
