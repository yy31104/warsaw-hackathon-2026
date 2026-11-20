import draftFixture from '../demo/rule-draft.json';
import {useState} from 'react';
import {useLocation,useNavigate} from 'react-router-dom';
import {useQuery,useQueryClient} from '@tanstack/react-query';
import {Button,Badge,Alert} from '@mantine/core';
import YAML from 'yaml';
import {PageTitle,Panel,ErrorBox,Data,Select,TextInput,Textarea,MultiSelect,Switch,useApp} from './App';
import {MOCK_MODE,request} from './api';
import {SafetyChecker,ImpactResults,SchemaVerification} from './VerificationPanels';
import {ImpactPreview,type ImpactPreviewResult} from './ImpactPreview';
import {eventPaths} from './verification';
import {record,useSession} from './session';
import {ownsRule} from './permissions';
import {store,persist} from './store';
import {sentence,render,evaluate} from './ruleLogic';
import type {Rule,Node,Preference} from './types';
import issues from '../../fixtures/api/schema_issues.json';
import suggestion from '../../fixtures/api/schema_suggest.json';
import funnel from '../../fixtures/api/funnel.json';
const operators=['eq','neq','in','not_in','gt','gte','lt','lte','glob','regex','contains','exists','not_exists'];
const paths=eventPaths;
function fresh():Rule{return structuredClone(draftFixture) as Rule;}
function Conditions({node,onChange,disabled=false}:{node:Node;onChange:(n:Node)=>void;disabled?:boolean}){if('field'in node)return <div className="condition"><TextInput label="Field" list="field-paths" value={node.field} disabled={disabled} onChange={e=>onChange({...node,field:e.target.value})}/><Select label="Operator" data={operators.map(op=>({value:op,label:({eq:'Is equal to',neq:'Is not equal to',glob:'Matches pattern (*)',gte:'At least',lte:'At most',gt:'Greater than',lt:'Less than',exists:'Is present',not_exists:'Is missing',contains:'Contains',regex:'Regular expression',in:'Is one of',not_in:'Is not one of'} as Record<string,string>)[op]??op}))} value={node.op} disabled={disabled} onChange={v=>onChange({...node,op:v??'eq'})}/><TextInput label="Value" value={String(node.value??'')} disabled={disabled||['exists','not_exists'].includes(node.op)} onChange={e=>onChange({...node,value:e.target.value})}/></div>;const list=node.all??node.any??[];return <div className="condition-group"><Select label="Match conditions" data={[{value:'all',label:'Match all conditions (AND)'},{value:'any',label:'Match any condition (OR)'}]} disabled={disabled} value={node.all?'all':'any'} onChange={v=>onChange({[v??'all']:list})}/>{list.map((n,i)=><div key={i}><Conditions node={n} disabled={disabled} onChange={value=>onChange({[node.all?'all':'any']:list.map((old,j)=>j===i?value:old)})}/>{!disabled&&<Button size="xs" variant="subtle" color="red" onClick={()=>onChange({[node.all?'all':'any']:list.filter((_,j)=>j!==i)})}>Remove</Button>}</div>)}{!disabled&&<div className="actions"><Button variant="default" size="xs" onClick={()=>onChange({[node.all?'all':'any']:[...list,{field:'$severity',op:'eq',value:'critical'}]})}>+ Condition</Button><Button variant="default" size="xs" onClick={()=>onChange({[node.all?'all':'any']:[...list,{any:[{field:'$service',op:'eq',value:'payments-api'}]}]})}>+ Nested group</Button></div>}</div>;}
export function Rules(){const {actor,admin,directory,notice}=useApp();const location=useLocation();const[rule,setRule]=useState<Rule|null>(()=>location.state?.sample?{...fresh(),owner:`user:${actor}`,match:{all:[{field:Object.keys(location.state.sample)[0],op:'eq',value:String(Object.values(location.state.sample)[0])}]}}:null);const queryClient=useQueryClient();const [baseRule,setBaseRule]=useState<Rule|null>(null),[saving,setSaving]=useState(false),[draftInfo,setDraftInfo]=useState<{source:string;warnings:string[]}|null>(null);const[yaml,setYaml]=useState(''),[tab,setTab]=useState('builder'),[error,setError]=useState(''),[test,setTest]=useState<unknown>(null),[preview,setPreview]=useState<{rule:Rule;result:ImpactPreviewResult}|true|null>(null),[nl,setNl]=useState(''),[sample,setSample]=useState(JSON.stringify(location.state?.sample??{'$schema':'trade.rejected.v2','$severity':'warning','payload.desk':'fx','payload.notional':25000000},null,2));const query=useQuery({queryKey:['rules'],queryFn:async()=>MOCK_MODE?store.rules:(await request<{items:Rule[]}>('/rules')).items});const readonly=rule?.origin==='file'||!!rule&&!ownsRule(rule.owner,actor,admin,directory);
 function edit(r:Rule,base:Rule|null=null){
  setRule(structuredClone(r));
  setBaseRule(base);
  setYaml(YAML.stringify(r));
  setDraftInfo(null);
  setError('');
  setPreview(null);
  setTest(null);
 }
 async function save(){
  if(!rule||readonly||saving)return;
  setSaving(true);
  setError('');
  try{
   if(!rule.id.trim()||!rule.route.length||rule.route.some(r=>!r.to||!r.channels.length)){
    throw new Error('Add a rule ID, recipient and at least one channel.');
   }
   let saved:Rule;
   if(MOCK_MODE){
    const old=store.rules.find(r=>r.id===rule.id);
    if(old&&!ownsRule(old.owner,actor,admin,directory))throw new Error('Rule ownership is required to overwrite this ID');
    if(old?.origin==='file')throw new Error('Choose a new ID; file rules are read-only');
    saved={...rule,version:(old?.version??0)+1};
    store.rules=store.rules.filter(r=>r.id!==rule.id).concat(saved);
    persist();
    record(`${actor} saved rule ${saved.id} v${saved.version}`,'config');
   }else{
    const original=baseRule?.id===rule.id?baseRule:null;
    const draft={...rule,...(draftInfo?{source:draftInfo.source}:{})};
    saved=original
     ?await request<Rule>(`/rules/${encodeURIComponent(original.id)}`,actor,{...draft,version:original.version},'PUT')
     :await request<Rule>('/rules',actor,draft,'POST');
   }
   edit(saved,saved);
   void queryClient.invalidateQueries({queryKey:['rules']});
   notice(`Rule saved · ${saved.id} v${saved.version}`);
  }catch(e){setError(String(e));}
  finally{setSaving(false);}
 }
 return <><PageTitle title="Rules" description="Choose when to alert, who to notify, and how the message looks."/><div className="creation-options"><Panel title="1. Build with rule editor"><p>Select conditions, recipients and channels step by step. No written description required.</p><Button onClick={()=>edit({...fresh(),owner:`user:${actor}`})}>Create using rule editor</Button></Panel><Panel title="2. Describe in natural language"><p>Optional shortcut: generate a draft, then review it in the rule editor.</p><Textarea label="Plain-English request" placeholder="Notify Anna via SMS when severity is critical and service is payments-api" value={nl} onChange={e=>setNl(e.target.value)}/><Button disabled={!nl.trim()} onClick={async()=>{try{if(MOCK_MODE){edit({...fresh(),owner:`user:${actor}`});notice('Mock draft: trade.rejected on desk fx. Review the generated conditions.');}else{const result=await request<{draft:Rule;source:string;warnings:string[]}>('/rules/draft',actor,{text:nl});edit(result.draft);setDraftInfo(result);}}catch(e){setError(String(e));}}}>Generate draft & open editor {MOCK_MODE?'(mock)':''}</Button></Panel></div><ErrorBox error={error||query.error}/>{!rule?<Panel title="Configured rules">{query.data?.map(r=><div className="row" key={r.id}><div><strong>{r.id}</strong><p>{r.owner} · v{r.version} · {r.enabled?'Enabled':'Paused'}</p><p>{r.plain_english??sentence(r)}</p></div><div><Badge color={r.origin==='file'?'gray':'blue'}>{r.origin}</Badge><Button variant="subtle" onClick={()=>edit(r,r)}>View {r.origin==='ui'?' / edit':''}</Button></div></div>)}</Panel>:<><div className="actions"><Button variant="subtle" onClick={()=>setRule(null)}>← All rules</Button>{readonly&&<Button variant="default" onClick={()=>edit({...rule,id:`${rule.id}-copy`,origin:'ui',version:1,owner:`user:${actor}`})}>Duplicate to edit</Button>}</div>{readonly&&<Alert color="gray">{rule.origin==='file'?'This rule comes from a configuration file and is read-only. Duplicate it to create your own rule.':'Only the rule owner or team approver can edit this rule. Duplicate it or use Admin simulation.'}</Alert>}<Panel title="Rule editor">{baseRule&&<Badge>v{baseRule.version}</Badge>}{draftInfo&&<><Badge>Draft source · {draftInfo.source}</Badge>{draftInfo.warnings.map(warning=><p key={warning}>{warning}</p>)}</>}<p className="summary-sentence">{sentence(rule)}</p><Switch label="Enable rule" checked={rule.enabled} disabled={readonly} onChange={event=>setRule({...rule,enabled:event.currentTarget.checked})}/><div className="actions"><Button variant={tab==='builder'?'filled':'default'} onClick={()=>setTab('builder')}>Visual builder</Button><Button variant={tab==='yaml'?'filled':'default'} onClick={()=>{setYaml(YAML.stringify(rule));setTab('yaml');}}>YAML</Button></div>{tab==='yaml'?<><Textarea label="Rule YAML" minRows={18} value={yaml} readOnly={readonly} onChange={e=>setYaml(e.target.value)}/><Button disabled={readonly} onClick={()=>{try{const parsed=YAML.parse(yaml) as Rule;if(!parsed?.id||!parsed.match||!Array.isArray(parsed.route))throw new Error('Rule requires id, match and route');setRule({...parsed,origin:'ui'});setTab('builder');}catch(e){setError(String(e));}}}>Apply YAML to builder</Button></>:<><div className="form-grid"><TextInput label="Rule ID" disabled={readonly} value={rule.id} onChange={e=>setRule({...rule,id:e.target.value})}/><TextInput label="Owner" disabled={readonly} value={rule.owner} onChange={e=>setRule({...rule,owner:e.target.value})}/></div><datalist id="field-paths">{paths.map(p=><option key={p} value={p}/>)}</datalist><Conditions node={rule.match} disabled={readonly} onChange={match=>setRule({...rule,match})}/><h3>Notify</h3>{rule.route.map((r,i)=><div className="form-grid" key={i}><TextInput label="Recipient (user:, team:, oncall:)" value={r.to} disabled={readonly} onChange={e=>setRule({...rule,route:rule.route.map((old,j)=>i===j?{...r,to:e.target.value}:old)})}/><MultiSelect label="Channels · empty uses preferences" disabled={readonly} data={['email','sms','phone','ui']} value={r.channels==='preferences'?[]:r.channels} onChange={channels=>setRule({...rule,route:rule.route.map((old,j)=>i===j?{...r,channels:channels.length?channels:'preferences'}:old)})}/>{!readonly&&<Button variant="subtle" color="red" onClick={()=>setRule({...rule,route:rule.route.filter((_,index)=>index!==i)})}>Remove recipient</Button>}</div>)}{!readonly&&<Button variant="default" size="xs" onClick={()=>setRule({...rule,route:rule.route.concat({to:`user:${actor}`,channels:'preferences'})})}>Add recipient</Button>}<h3>Presentation</h3><TextInput label="Title template" value={rule.present?.title??''} disabled={readonly} onChange={e=>setRule({...rule,present:{...rule.present,title:e.target.value}})}/><Textarea label="Body template" disabled={readonly} value={rule.present?.body??''} onChange={e=>setRule({...rule,present:{...rule.present,body:e.target.value}})}/><MultiSelect label="Displayed fields" searchable data={paths} value={rule.present?.fields??[]} disabled={readonly} onChange={fields=>setRule({...rule,present:{...rule.present,fields}})}/><Select label="Card colour" disabled={readonly} data={['red','orange','amber','blue','grey']} value={rule.present?.color??'blue'} onChange={color=>setRule({...rule,present:{...rule.present,color:color??'blue'}})}/></>}<h3>Test before saving</h3><Textarea label="Sample event · flattened JSON fields" value={sample} minRows={5} onChange={e=>setSample(e.target.value)}/><p><strong>Live title preview:</strong> {(()=>{try{return render(rule.present?.title??'',JSON.parse(sample));}catch{return 'Enter valid sample JSON';}})()}</p><div className="actions"><Button variant="default" onClick={async()=>{try{const event=JSON.parse(sample);setTest(MOCK_MODE?{source:'local mock evaluator',matched:evaluate(rule.match,event),conditions:sentence(rule)}:await request('/rules/test',actor,{rule,event}));}catch(e){setError(String(e));}}}>Test against event</Button><Button variant="default" onClick={async()=>{try{setError('');setPreview(null);setPreview(MOCK_MODE?true:{rule,result:await request<ImpactPreviewResult>(`/rules/${encodeURIComponent(rule.id)}/preview`,actor,{draft:rule,last_n:2000})});}catch(e){setError(String(e));}}}>Preview impact</Button><Button disabled={readonly} loading={saving} onClick={()=>void save()}>Save rule</Button></div>{test!=null&&<Data value={test}/ >}{preview===true?<><h3>Impact preview · local sample</h3><ImpactResults current={store.rules.find(r=>r.id===rule.id)} draft={rule} sample={sample}/></>:preview?.rule===rule&&<ImpactPreview result={preview.result}/>}<SafetyChecker rule={rule} sample={sample}/></Panel></>}</>;
}
export function People(){
 const {actor,admin}=useApp();
 return <PreferenceEditor key={`${actor}:${admin}`}/>;
}
function PreferenceEditor(){
 const {actor,admin,directory,notice}=useApp();
 const [user,setUser]=useState(actor);
 const [draft,setDraft]=useState<Preference[]|null>(null);
 const [error,setError]=useState('');
 const [saving,setSaving]=useState(false);
 const queryClient=useQueryClient();
 const path=`/users/${encodeURIComponent(user)}/preferences`;
 const saved=useQuery({
  queryKey:['preferences',user],
  queryFn:()=>request<Preference[]>(path,actor),
  enabled:!MOCK_MODE,
 });
 const rows=draft??(MOCK_MODE
  ?store.preferences[user]??directory.users.find(person=>person.id===user)?.preferences??directory.default_preferences
  :saved.data);
 async function save(){
  if(!rows||saving)return;
  setSaving(true);
  setError('');
  try{
   if(rows.some(row=>!row.channels.length||row.when&&!/^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$/.test(row.when))){
    throw new Error('Select channels and use HH:MM-HH:MM for time windows.');
   }
   if(MOCK_MODE){
    store.preferences[user]=structuredClone(rows);
    persist();
    record(`${actor} updated ${user}'s notification preferences`,'config');
   }else{
    queryClient.setQueryData(['preferences',user],await request<Preference[]>(path,actor,rows,'PUT'));
   }
   setDraft(null);
   notice(`${user}'s preferences saved`);
  }catch(e){setError(String(e));}
  finally{setSaving(false);}
 }
 return <>
  <PageTitle title="People & preferences" description="Your first matching preference is used. Times follow your profile time zone."/>
  <ErrorBox error={error||saved.error}/>
  <Panel title={admin?'Admin · assign notification preferences':'My notification preferences'}>
   <p>{admin?'Mock admin can assign preferences to any person.':`Editing your own preferences as ${actor}.`}</p>
   <Select disabled={!admin||saving} label="Person" value={user} data={directory.users.map(person=>person.id)} onChange={value=>{
    setUser(value??actor);
    setDraft(null);
    setError('');
   }}/>
   {rows?.map((row,index)=><div className="preference" key={index}>
    <strong>{index+1}</strong>
    <TextInput label="Time window · blank = anytime" placeholder="09:00-18:00" value={row.when??''} onChange={event=>setDraft(rows.map((item,position)=>index===position?{...row,when:event.target.value||undefined}:item))}/>
    <Select label="Minimum severity" data={['debug','info','warning','error','critical']} value={row.min_severity} onChange={value=>setDraft(rows.map((item,position)=>index===position?{...row,min_severity:value??'critical'}:item))}/>
    <MultiSelect label="Channels" searchable data={['sms','email','phone','ui']} value={row.channels} onChange={channels=>setDraft(rows.map((item,position)=>index===position?{...row,channels}:item))}/>
    <Button disabled={!index} aria-label="Move preference up" variant="subtle" onClick={()=>{const reordered=[...rows];[reordered[index-1],reordered[index]]=[reordered[index],reordered[index-1]];setDraft(reordered);}}>↑</Button>
    <Button color="red" variant="subtle" onClick={()=>setDraft(rows.filter((_,position)=>position!==index))}>Remove</Button>
   </div>)}
   {!rows&&!saved.error&&<p>Loading preferences…</p>}
   {rows&&<div className="actions">
    <Button variant="default" onClick={()=>setDraft([...rows,{min_severity:'critical',channels:['email']}])}>Add preference</Button>
    <Button loading={saving} onClick={()=>void save()}>Save preferences</Button>
   </div>}
  </Panel>
  <Panel title="Team defaults · who gets what">
   <p>Used when no personal preference matches.</p>
   <table><thead><tr><th>Severity</th><th>Channels</th></tr></thead><tbody>
    {directory.default_preferences.map(preference=><tr key={preference.min_severity}><td>{preference.min_severity}</td><td>{preference.channels.join(' + ')}</td></tr>)}
   </tbody></table>
   {directory.teams.map(team=><div className="row" key={team.id}><strong>{team.id}</strong><span>{team.members.join(', ')} · Approvers: {team.approvers.join(', ')}</span></div>)}
  </Panel>
 </>;
}
export function Onboarding(){const{actor,admin,notice}=useApp();const navigate=useNavigate();const[show,setShow]=useState(false),[saved,setSaved]=useState(store.schemaSaved),[draft,setDraft]=useState<typeof suggestion.draft>(()=>{try{return JSON.parse(localStorage.getItem('af-schema-draft')??'null')??suggestion.draft;}catch{return suggestion.draft;}}),[error,setError]=useState('');const data=useQuery({queryKey:['schema-issues'],queryFn:()=>MOCK_MODE?issues:request<typeof issues>('/schema-issues')});const issue=data.data?.items[0];return <><PageTitle title="Format onboarding" description="Inspect a sample, review the mapping, then create a routing rule."/><ErrorBox error={error||data.error}/>{issue?<><Panel title="1. Inspect unknown format"><Badge>{saved?'Mapping confirmed':'Needs review'}</Badge><p>{issue.source_id} · {issue.event_count} events</p><table><thead><tr><th>Field path</th><th>Type</th><th>Sample</th></tr></thead><tbody>{Object.entries(issue.sample_fields).map(([k,v])=><tr key={k}><td>{k}</td><td>{typeof v}</td><td>{String(v)}</td></tr>)}</tbody></table><Button onClick={async()=>{try{if(!MOCK_MODE){const result=await request<typeof suggestion>('/schemas/suggest',actor,{sample:issue.sample_fields});setDraft(result.draft);}setShow(true);}catch(e){setError(String(e));}}}>Suggest mapping {MOCK_MODE?'(mock)':''}</Button></Panel>{show&&<Panel title="2. Review mapping"><Badge color="orange">{MOCK_MODE?'Mock AI suggestion · fixture':'Draft suggestion'}</Badge><TextInput label="Schema ID" value={draft.id} onChange={e=>setDraft({...draft,id:e.target.value})}/><TextInput label="Name template" value={draft.name} onChange={e=>setDraft({...draft,name:e.target.value})}/>{(['severity','service','environment','time','error_code','message'] as const).map(alias=><TextInput key={alias} label={`$${alias} alias`} description={suggestion.rationale[alias as keyof typeof suggestion.rationale]} value={(draft.aliases as Record<string,string>)[alias]??''} onChange={e=>setDraft({...draft,aliases:{...draft.aliases,[alias]:e.target.value}})}/>)}<Select label="Default severity" data={['critical','error','warning','info','unknown']} value={draft.severity_default} onChange={v=>setDraft({...draft,severity_default:v??'error'})}/><SchemaVerification aliases={draft.aliases} defaultSeverity={draft.severity_default}/><details><summary>Detection and grouping</summary><Data value={draft}/></details><Button disabled={saved||(!admin&&actor!=='ops')} onClick={async()=>{try{if(!draft.id.trim())throw new Error('Schema ID is required');if(!MOCK_MODE)await request('/schemas',actor,draft);store.schemaSaved=true;localStorage.setItem('af-schema-draft',JSON.stringify(draft));persist();record(`${actor} confirmed schema ${draft.id}`,'schema');setSaved(true);notice('Schema mapping confirmed');}catch(e){setError(String(e));}}}>Confirm mapping</Button>{!admin&&actor!=='ops'&&<p>Mapping confirmation is available to platform ops or Admin simulation. You may inspect and draft suggestions.</p>}</Panel>}{saved&&<Panel title="3. Route new events"><Button onClick={()=>navigate('/rules',{state:{sample:issue.sample_fields}})}>Create rule from this sample</Button><p>First routed alert: NOT RUN. Real ingest and routing are not connected in mock mode.</p></Panel>}</>:<p>{data.isPending?'Loading…':'No unknown formats need review.'}</p>}</>;}
export function Dashboard(){const state=useSession();const data=useQuery({queryKey:['funnel'],queryFn:()=>MOCK_MODE?funnel:request<typeof funnel>('/stats/funnel?window=15m')});const d=data.data?{...data.data,...(MOCK_MODE?{alerts_open:state.alerts.filter(a=>a.status!=='resolved').length,critical:state.alerts.filter(a=>a.status!=='resolved'&&a.severity==='critical').length,notifications:{delivered:state.notifications.filter(n=>n.state==='delivered').length,retrying:state.notifications.filter(n=>n.state==='retrying').length,failed:state.notifications.filter(n=>n.state==='failed').length}}:{})}:undefined;return <><PageTitle title="Funnel dashboard" description="A concise view of volume, grouping and delivery health."/><ErrorBox error={data.error}/>{d&&<><Alert color="gray">{MOCK_MODE?'Raw / accepted / throughput are fixture snapshots; incident and delivery counts reflect this mock session. Not measured performance.':'API-reported values.'} Window: {d.window} · Benchmark: NOT RUN.</Alert><Panel title="Events → alerts → notifications"><div className="funnel">{[['Raw events',d.raw],['Accepted',d.accepted],['Open incidents',d.alerts_open],['Delivered',d.notifications.delivered]].map(([label,value])=><div className={label==='Open incidents'||label==='Critical'?'attention-metric':''} key={label}><strong>{Number(value).toLocaleString()}</strong><span>{label}</span></div>)}</div><p>These counts represent different entities, not conversion rates.</p></Panel><Panel title="Critical alerts · attention"><strong className="critical-count">{d.critical}</strong><p>Severity classification, separate from the event-to-notification flow.</p></Panel><Panel title="Processing health"><div className="stats">{[['Ingest / s',d.ingest_per_s],['Processed / s',d.processed_per_s],['Backlog',d.backlog],['Oldest pending / s',d.oldest_pending_s]].map(([label,value])=><div key={label}><strong>{value}</strong>{label}</div>)}</div></Panel><Panel title="Delivery"><div className="row"><span>Delivered</span><strong>{d.notifications.delivered}</strong></div><div className="row"><span>Retrying notifications</span><strong>{d.notifications.retrying}</strong></div><div className="row"><span>Failed</span><strong>{d.notifications.failed}</strong></div><p>Retries in the last five minutes: {'retries_5m' in d?String(d.retries_5m):'—'} · Duplicates: {d.duplicates} · Needs review: {d.needs_review}</p></Panel></>}</>;}
