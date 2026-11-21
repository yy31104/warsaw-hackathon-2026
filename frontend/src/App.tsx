import {useEffect,useState,createContext,useContext} from 'react';
import {Routes,Route,Link,useParams,useSearchParams} from 'react-router-dom';
import {Button,Select,TextInput,Badge,Alert,Textarea,MultiSelect,Switch} from '@mantine/core';
import {QueryClient,QueryClientProvider,useQuery,useQueryClient} from '@tanstack/react-query';
import {ProposalPage} from './ProposalPage';
import {Overview,Approvals} from './Overview';
import {WorkspaceShell} from './WorkspaceShell';
import {ruleAccent, SeverityBadge} from './ActiveAlerts';
import {useSession,record} from './session';
import {api,alertListQuery,request,MOCK_MODE,ALERTS_MOCK,subscribe} from './api';
import {filterAlerts} from './model';
import {directory as mockDirectory} from './store';
import type {Directory} from './types';
import {Rules,People,Onboarding,Dashboard} from './Screens';
import {RunbookPanel} from './Runbook';
import {RunbookCatalogue} from './RunbookCatalogue';
import {SchemaOnboarding} from './SchemaOnboarding';
import {inTeam,priorityLabel} from './permissions';
import {store,persist} from './store';
export {Select,TextInput,Textarea,MultiSelect,Switch};
const queryClient = new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
const emptyDirectory:Directory={teams:[],users:[],default_preferences:[]};
const Context=createContext({actor:'sam',team:'',feedEnabled:true,admin:false,directory:mockDirectory,notice:console.info});
export const useApp=()=>useContext(Context);
export function PageTitle({title,description,action}:{title:string;description?:string;action?:React.ReactNode}) {
 return <div className="page-title"><div><h1>{title}</h1>{description&&<p>{description}</p>}</div>{action}</div>;
}
export function Panel({title,children}:{title:string;children:React.ReactNode}){return <section className="panel"><h2>{title}</h2>{children}</section>;}
export function Data({value}:{value:unknown}){return <pre>{JSON.stringify(value,null,2)}</pre>;}
export function ErrorBox({error}:{error:unknown}){return error?<Alert color="red" title="Could not complete request">{String(error)}</Alert>:null;}
function App(){
 const state=useSession();
 const [admin,setAdmin]=useState(false);
 const [actor,setActor]=useState(()=>sessionStorage.getItem('af-actor')??'sam');
 const [selectedTeam,setTeam]=useState(()=>sessionStorage.getItem('af-team')??'');
 const [live,setLive]=useState('Connecting'),[last,setLast]=useState('No events yet'),[toast,setToast]=useState('');
 const query=useQueryClient();
 const config=useQuery({queryKey:['directory'],queryFn:()=>request<Directory>('/teams'),enabled:!MOCK_MODE});
 const directory=MOCK_MODE?mockDirectory:config.data??emptyDirectory;
 const teams=MOCK_MODE?directory.teams:directory.teams.filter(team=>team.members.includes(actor));
 const team=MOCK_MODE||teams.some(team=>team.id===selectedTeam)?selectedTeam:teams[0]?.id??'';
 const feedEnabled=MOCK_MODE||Boolean(team);
 const risk=useQuery({queryKey:['delivery-risk',team],queryFn:async()=>({at_risk:false}),enabled:false});
 useEffect(()=>{
  setLast('No events yet');
  if(!feedEnabled){setLive('Waiting for a team');return;}
  return subscribe(message=>{
  setLast(new Date().toLocaleTimeString());
  if(message.type==='stats.tick')query.setQueryData(['delivery-risk',team],message.data);
  if(message.type.startsWith('alert.'))void query.invalidateQueries({queryKey:['alerts']});
  if(message.type==='notification.updated'||message.type==='action.updated'){
   void query.invalidateQueries({queryKey:['detail']});
   void query.invalidateQueries({queryKey:['alerts','detail']});
  }
  if(message.type==='schema.issue')void query.invalidateQueries({queryKey:['schema-issues']});
  if(message.type==='config.changed'){
   for(const key of ['directory','rules','preferences','runbooks','proposals','schema-issues']){
    void query.invalidateQueries({queryKey:[key]});
   }
  }
  },status=>{setLive(status);if(status==='Live')void query.invalidateQueries();},team);
 },[query,team,feedEnabled]);
 useEffect(()=>{if(MOCK_MODE){void query.invalidateQueries({queryKey:['alerts']});void query.invalidateQueries({queryKey:['detail']});}},[query,state.activity.length,state.actions.length]);
 useEffect(()=>{if(!toast)return;const timer=setTimeout(()=>setToast(''),5000);return()=>clearTimeout(timer);},[toast]);
 return <Context.Provider value={{actor,team,feedEnabled,admin,directory,notice:setToast}}>
  <WorkspaceShell live={live} last={last} risk={risk.data?.at_risk} controls={<>
   <label className="scope-field"><span>Team</span><select aria-label="Filter by team" value={team}
    onChange={event=>{const value=event.target.value;sessionStorage.setItem('af-team',value);setTeam(value);}}>
    {(MOCK_MODE||!teams.length)&&<option value="">{MOCK_MODE?'All teams':'No team'}</option>}
    {teams.map(item=><option key={item.id} value={item.id}>{item.id}</option>)}
   </select></label>
   <label className="scope-field"><span>Viewing as</span><select aria-label="Acting as · simulated auth" value={actor}
    onChange={event=>{const value=event.target.value;sessionStorage.setItem('af-actor',value);setActor(value);}}>
    {!directory.users.some(user=>user.id===actor)&&<option value={actor}>{actor}</option>}
    {directory.users.map(user=><option key={user.id} value={user.id}>{user.id}</option>)}
   </select></label>
   {MOCK_MODE&&<label className="scope-field"><span>Demo role</span><select aria-label="Simulation mode" value={admin?'admin':'user'}
    onChange={event=>setAdmin(event.target.value==='admin')}><option value="user">User</option><option value="admin">Admin</option></select></label>}
  </>}>
   <ErrorBox error={config.error}/>
   {(MOCK_MODE||ALERTS_MOCK)&&<div className="demo-notice">{MOCK_MODE&&ALERTS_MOCK?'Demo workspace · Simulated alerts and actions':ALERTS_MOCK?'Demo alerts · API configuration':'API alerts · Demo configuration'}{admin?' · Admin simulation':''}</div>}
   <Routes>
    <Route path="/overview" element={<Overview/>}/>
    <Route path="/alerts" element={<Feed/>}/>
    <Route path="/approvals" element={<Approvals/>}/>
    <Route path="/runbooks" element={MOCK_MODE?<Panel title="Runbook formats"><p>Explore configured runbook formats and simulated actions in the settlement incident.</p><Link to="/a/101">Open settlement runbook →</Link></Panel>:<RunbookCatalogue/>}/>
    <Route path="/a/:id" element={<Detail/>}/>
    <Route path="/rules" element={<Rules/>}/>
    <Route path="/people" element={<People/>}/>
    <Route path="/onboarding" element={MOCK_MODE?<Onboarding/>:<SchemaOnboarding/>}/>
    <Route path="/proposals" element={<ProposalPage/>}/>
    <Route path="/dashboard" element={<Dashboard/>}/>
    <Route path="*" element={<Panel title="Page not found"><Link to="/overview">Return to overview</Link></Panel>}/>
   </Routes>
  </WorkspaceShell>
  {toast&&<div role="status" className="toast">✓ {toast}</div>}
 </Context.Provider>;
}
function Feed() {
 const {team,feedEnabled}=useApp();
 const [params,setParams]=useSearchParams();
 const [q,setQ]=useState('');
 const status=params.get('status')??'';
 const severity=params.get('severity')??'';
 const data=useQuery(alertListQuery(team,feedEnabled));
 const items=filterAlerts(data.data??[],{team,status,severity,q});
 function changeFilter(key:string,value:string|null) {
  const next=new URLSearchParams(params);
  if(value)next.set(key,value);else next.delete(key);
  setParams(next,{replace:true});
 }
 return <>
  <PageTitle title="Live alerts" description="Open an alert to see what happened, who was notified and what to do next."/>
  <div className="stats">
   <div><strong>{data.data?.filter(a=>a.status==='open').length??'—'}</strong>Open alerts</div>
   <div><strong>{data.data?.filter(a=>a.status==='acknowledged').length??'—'}</strong>Acknowledged</div>
   <div><strong>{data.data?.reduce((s,a)=>s+a.occurrence_count,0).toLocaleString()??'—'}</strong>Total occurrences</div>
  </div>
  <div className="filterbar">
   <TextInput aria-label="Search alerts" placeholder="Search alerts or rules…" value={q} onChange={e=>setQ(e.target.value)}/>
   <Select aria-label="Status" placeholder="All statuses" clearable data={['active','open','acknowledged','resolved']}
    value={status||null} onChange={value=>changeFilter('status',value)}/>
   <Select aria-label="Severity" placeholder="All severities" clearable data={['critical','error','warning','info','unknown']}
    value={severity||null} onChange={value=>changeFilter('severity',value)}/>
  </div>
  <ErrorBox error={data.error}/>
  {!feedEnabled?<p>Choose a user with a team to view alerts.</p>:data.isPending?<p>Loading alerts…</p>:data.data&&<>
   <div className="list-heading"><span>{items.length} {items.length===1?'alert':'alerts'}</span><span>Sorted by response priority</span></div>
   {items.map(a=><Link className={`incident ${a.color}`} key={a.id} to={`/a/${a.id}`} style={{borderInlineStartColor:ruleAccent(a.color)}}>
    <div><div className="badges"><SeverityBadge value={a.severity}/>
     <Badge variant="light" color="gray">{a.status}</Badge><span>{a.team_id}</span>
    </div><h2>{a.title}</h2><p>#{a.id} · {a.rule_id} · First seen {new Date(a.first_seen_at).toLocaleString()}</p></div>
    <div className="counter"><strong>{a.occurrence_count.toLocaleString()}</strong><span>{a.occurrence_count===1?'occurrence':'occurrences'}</span>
     <small>{priorityLabel(a.priority)} priority {a.priority&&`(${a.priority})`}</small><small>Ranking score: {a.score??'unknown'}</small>
    </div>
   </Link>)}
   {items.length===0&&<Panel title={q||status||severity?'No matching alerts':'No alerts for this team'}>
    <p>{q||status||severity?'Try another search or clear your filters.':'Alerts will appear here when an event matches a routing rule.'}</p>
    {(q||status||severity)&&<Button variant="default" onClick={()=>{setQ('');setParams({},{replace:true});}}>Clear filters</Button>}
   </Panel>}
  </>}
 </>;
}
function Detail(){const{id}=useParams();const number=Number(id);const{actor,admin,directory,notice}=useApp();const client=useQueryClient();const[error,setError]=useState(''),[busy,setBusy]=useState(false),[reason,setReason]=useState(''),[proposal,setProposal]=useState(false);const detail=useQuery({queryKey:['alerts','detail',number],queryFn:()=>api.detail(number)});const notifications=useQuery({queryKey:['detail','notifications',number],queryFn:()=>api.notifications(number)});const[explain,setExplain]=useState<unknown>(null);const a=detail.data;async function act(action:'ack'|'resolve'){if(!a||!inTeam(a.team_id,actor,admin,directory)){setError('Only team members can change this incident. Use Admin simulation to test other teams.');return;}setBusy(true);setError('');try{await api.transition(number,action,actor);await client.invalidateQueries({queryKey:['alerts']});notice(MOCK_MODE?'Mock incident updated':'Incident updated');}catch(e){setError(String(e));}finally{setBusy(false);}}
 if(detail.error)return <ErrorBox error={detail.error}/>;if(!a)return <p>Loading incident…</p>;
 return <><Link to="/alerts" className="back">← Live alerts</Link><PageTitle title={a.title} description={`Incident #${a.id} · ${a.team_id} · ${a.occurrence_count.toLocaleString()} ${a.occurrence_count===1?'occurrence':'occurrences'}`} action={<div className="actions"><Button loading={busy} disabled={a.status!=='open'||!inTeam(a.team_id,actor,admin,directory)} variant="default" onClick={()=>void act('ack')}>Acknowledge</Button><Button loading={busy} disabled={a.status==='resolved'||!inTeam(a.team_id,actor,admin,directory)} onClick={()=>void act('resolve')}>Resolve incident</Button></div>}/><ErrorBox error={error}/><div className="badges"><SeverityBadge value={a.severity}/>
<Badge>{a.status}</Badge></div><details className="rank-detail"><summary>{priorityLabel(a.priority)} response priority {a.priority&&`(${a.priority})`}</summary>
  <p>The ranking score is {a.score??'unknown'}. It helps order alerts within a priority level. P0 is immediate, P1 high, P2 medium and P3 low.</p></details>
  <div className="detail-grid"><div><Panel title="What happened">{a.body&&<p>{a.body}</p>}{!a.body&&!Object.keys(a.fields).length&&<p>No additional details recorded.</p>}<dl>{Object.entries(a.fields).map(([k,v])=><div key={k}><dt>{k}</dt><dd>{String(v)}</dd></div>)}</dl><details><summary>Why did I get this?</summary><p>Rule: <strong>{a.rule_id}</strong>. Grouped by <code>{a.alert_key}</code>.</p>{ALERTS_MOCK?<><p>Illustrative trace from the configured rule. Not a measured backend evaluation.</p>{(store.rules.find(r=>r.id===a.rule_id)?.match as {all?:{field:string;value?:unknown}[]})?.all?.map((c,i)=><p key={i}>✓ {c.field} matched {String(c.value)}</p>)}<p>Notifications below show the fixture recipients and channels.</p></>:<Button onClick={async()=>{try{setExplain(await request(`/events/${a.occurrences[0]?.event_id}/explain`));}catch(e){setError(String(e));}}} disabled={!a.occurrences.length}>Load decision trace</Button>}{explain!=null&&<Data value={explain}/>}</details></Panel>
  <Panel title="Delivery"><ErrorBox error={notifications.error}/>{notifications.data?.length===0&&<p>No notifications recorded.</p>}{notifications.data?.map(n=><div className="row" key={n.id}><div><strong>{n.channel.toUpperCase()} → {n.recipient}</strong><p>{n.attempts} {n.attempts===1?'attempt':'attempts'}</p><details><summary>Attempt history</summary><Data value={n.attempt_log}/></details></div><Badge color={n.state==='delivered'?'teal':'orange'}>{n.state}</Badge></div>)}</Panel>
  <Panel title="Activity timeline">{a.timeline.length?a.timeline.map((t,i)=><div className="timeline-item" key={i}><small>{new Date(t.ts).toLocaleString()} · {t.kind}</small><p>{t.text}</p></div>):<p>No activity recorded.</p>}</Panel>
  <Panel title="Recent occurrences"><table><thead><tr><th>Event</th><th>Received</th><th>Fields</th></tr></thead><tbody>{a.occurrences.map(o=><tr key={o.event_id}><td>{o.event_id}</td><td>{new Date(o.seen_at).toLocaleTimeString()}</td><td><details><summary>View fields</summary><Data value={o.fields}/></details></td></tr>)}</tbody></table>{!a.occurrences.length&&<p>No occurrence samples available.</p>}</Panel>
  <Panel title="Reduce noise"><Button variant="default" onClick={()=>setProposal(!proposal)}>Propose fix</Button>{proposal&&<><Textarea label="What should change?" value={reason} onChange={e=>setReason(e.target.value)}/><Button disabled={!reason.trim()} onClick={async()=>{if(!MOCK_MODE){try{await request('/proposals',actor,{alert_id:a.id,text:reason});notice(`Proposal sent to the owner of ${a.rule_id}`);setProposal(false);}catch(e){setError(String(e));}return;}const rule=store.rules.find(r=>r.id===a.rule_id);if(!rule){setError('No configurable rule found for this incident');return;}store.proposals.push({id:Date.now(),actor,reason,rule:structuredClone(rule),state:'pending'});persist();record(`${actor} proposed a change to ${rule.id}`,'config',a.id);notice('Mock proposal created. Review in Proposals.');setProposal(false);}}>{MOCK_MODE?'Create mock proposal':'Submit proposal'}</Button></>}</Panel></div><RunbookPanel id={number} teamId={a.team_id}/></div></>;}
export default function Workspace() {
 return <QueryClientProvider client={queryClient}><App/></QueryClientProvider>;
}
