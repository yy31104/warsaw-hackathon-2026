import {useEffect,useState,createContext,useContext} from 'react';
import {NavLink,Routes,Route,Link,useParams,useLocation} from 'react-router-dom';
import {Button,Select,TextInput,Badge,Alert,Textarea,MultiSelect,Switch,useMantineColorScheme} from '@mantine/core';
import {useQuery,useQueryClient} from '@tanstack/react-query';
import {ProposalPage} from './ProposalPage';
import {Overview,Approvals} from './Overview';
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
import {ConsoleShell} from './console/Shell';
import {readSession,writeSession} from './console/Intro';
import {Now} from './console/Now';
import {Incident} from './console/Incident';
import {Sources} from './console/Sources';
import {ConsoleRules} from './console/Rules';
import {ConsoleRunbooks} from './console/Runbooks';
import {OnCall} from './console/OnCall';
import {Health} from './console/Health';
import {detailQuery,representativeExplainPath} from './console/data';
import {LegacyLink} from './console/ui';
export {Select,TextInput,Textarea,MultiSelect,Switch};
const emptyDirectory:Directory={teams:[],users:[],default_preferences:[]};
const Context=createContext({actor:'sam',team:'',feedEnabled:true,admin:false,directory:mockDirectory,notice:console.info});
export const useApp=()=>useContext(Context);
export function PageTitle({title,description,action}:{title:string;description:string;action?:React.ReactNode}){return <div className="page-title"><div><div className="eyebrow">WORKSPACE / OPERATIONS</div><h1>{title}</h1><p>{description}</p></div>{action}</div>;}
export function Panel({title,children}:{title:string;children:React.ReactNode}){return <section className="panel"><h2>{title}</h2>{children}</section>;}
export function Data({value}:{value:unknown}){return <pre>{JSON.stringify(value,null,2)}</pre>;}
export function ErrorBox({error}:{error:unknown}){return error?<Alert color="red" title="Could not complete request">{String(error)}</Alert>:null;}
function App(){
 const location=useLocation();
 const state=useSession();
 const [admin,setAdmin]=useState(false);
 const [actor,setActor]=useState(()=>readSession('af-actor')??'sam');
 const [selectedTeam,setTeam]=useState(()=>readSession('af-team')??'');
 const [live,setLive]=useState('Connecting'),[last,setLast]=useState('No events yet'),[toast,setToast]=useState('');
 const query=useQueryClient();
 const config=useQuery({queryKey:['directory'],queryFn:async()=>{
  const result=await request<Directory>('/teams');
  return {...result,teams:result.teams.map(item=>({...item,approvers:item.approvers??[]}))};
 },enabled:!MOCK_MODE});
 const directory=MOCK_MODE?mockDirectory:config.data??emptyDirectory;
 const teams=MOCK_MODE?directory.teams:directory.teams.filter(team=>team.members.includes(actor)||team.approvers.includes(actor));
 const team=MOCK_MODE||teams.some(team=>team.id===selectedTeam)?selectedTeam:teams[0]?.id??'';
 const feedEnabled=MOCK_MODE||Boolean(team);
 const risk=useQuery({queryKey:['delivery-risk',team],queryFn:async()=>({at_risk:false}),enabled:false});
 const {colorScheme,toggleColorScheme}=useMantineColorScheme();
 useEffect(()=>{
  setLast('No events yet');
  if(!feedEnabled){setLive('Waiting for a team');return;}
  return subscribe(message=>{
  setLast(new Date().toLocaleTimeString());
  if(message.type==='stats.tick')query.setQueryData(['delivery-risk',team],message.data);
  if(message.type.startsWith('alert.')){
   void query.invalidateQueries({queryKey:['alerts']});
   void query.invalidateQueries({queryKey:['console-funnel']});
  }
  if(message.type==='notification.updated'||message.type==='action.updated'){
   void query.invalidateQueries({queryKey:['detail']});
   void query.invalidateQueries({queryKey:['alerts','detail']});
  }
  if(message.type==='action.updated')void query.invalidateQueries({queryKey:['pending-approvals']});
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
 if(!MOCK_MODE) {
  const context={actor,team,feedEnabled,admin:false,directory,notice:setToast};
  const chooseActor=(value:string)=>{writeSession('af-actor',value);setActor(value);};
  const chooseTeam=(value:string)=>{writeSession('af-team',value);setTeam(value);};
  return <Context.Provider value={context}>
   <ConsoleShell live={live} teams={teams} setActor={chooseActor} setTeam={chooseTeam}>
    <ErrorBox error={config.error}/>
    <Routes>
     <Route path="/" element={<Now/>}/>
     <Route path="/a/:id" element={<Incident/>}/>
     <Route path="/sources" element={<Sources/>}/>
     <Route path="/rules" element={<ConsoleRules/>}/>
     <Route path="/runbooks" element={<ConsoleRunbooks/>}/>
     <Route path="/oncall" element={<OnCall/>}/>
     <Route path="/health" element={<Health/>}/>
     <Route path="/rules/proposals" element={<Legacy back="/rules" label="Rules"><ProposalPage/></Legacy>}/>
     <Route path="/proposals" element={<Legacy back="/rules" label="Rules"><ProposalPage/></Legacy>}/>
     <Route path="/alerts" element={<Legacy back="/" label="Now"><Feed/></Legacy>}/>
     <Route path="/approvals" element={<Legacy back="/" label="Now"><Approvals/></Legacy>}/>
     <Route path="/overview" element={<Legacy back="/" label="Now"><Overview/></Legacy>}/>
     <Route path="/a/:id/details" element={<LegacyIncident/>}/>
     <Route path="/onboarding" element={<Legacy back="/sources" label="Sources"><SchemaOnboarding/></Legacy>}/>
     <Route path="/rules/advanced" element={<Legacy back="/rules" label="Rules"><Rules/></Legacy>}/>
     <Route path="/runbooks/catalogue" element={<Legacy back="/runbooks" label="Runbooks"><RunbookCatalogue/></Legacy>}/>
     <Route path="/people" element={<Legacy back="/oncall" label="On call"><People/></Legacy>}/>
     <Route path="/dashboard" element={<Legacy back="/health" label="Health"><Dashboard/></Legacy>}/>
     <Route path="*" element={<Panel title="Page not found"><Link to="/">Back to Now</Link></Panel>}/>
    </Routes>
    {toast&&<div role="status" className="cf-toast">{toast}</div>}
   </ConsoleShell>
  </Context.Provider>;
 }
 return <Context.Provider value={{actor,team,feedEnabled,admin,directory,notice:setToast}}><div className="shell horizontal-shell"><div className="workspace"><header className="top-navigation"><Link to="/" className="brand"><span className="brand-icon">⌁</span><span>Alert Funnel<small>EVENT OPERATIONS</small></span></Link><nav aria-label="Main navigation">{[['/','Overview'],['/alerts','Alerts'],['/dashboard','Funnel'],['/rules','Configuration'],['/onboarding','Onboarding'],['/approvals','Approvals']].map(([path,label])=><NavLink key={path} to={path} end={path==='/'} className={path==='/rules'&&['/people','/runbooks','/proposals'].includes(location.pathname)?'active':undefined}>{label}</NavLink>)}</nav><div className="top-controls"><Badge color="violet">{MOCK_MODE?'MOCK':'API'}</Badge><Button aria-label="Toggle theme" variant="subtle" onClick={()=>toggleColorScheme()}>{colorScheme==='dark'?'☼ Light':'◐ Dark'}</Button></div></header><div className="workspace-toolbar"><div className="stream"><span className="dot"/>{live}<small>Last event · {last}</small></div><div className={risk.data?.at_risk?'delivery-indicator delivery-at-risk':'delivery-indicator'}>{risk.data?.at_risk?'Delivery at risk':MOCK_MODE?`${state.notifications.filter(n=>['failed','retrying','unknown'].includes(n.state)).length} delivery risks · mock`:risk.data?'Delivery within target':'Delivery risk: unknown'}</div><Select aria-label="Filter by team" placeholder={MOCK_MODE?'All teams':'No team assigned'} clearable={MOCK_MODE} allowDeselect={MOCK_MODE} data={teams.map(t=>t.id)} value={team||null} onChange={v=>{const selected=v??'';sessionStorage.setItem('af-team',selected);setTeam(selected);}}/><Select aria-label="Simulation mode" value={admin?'admin':'user'} data={[{value:'user',label:'User simulation'},{value:'admin',label:'Admin · mock only',disabled:!MOCK_MODE}]} onChange={v=>setAdmin(v==='admin'&&MOCK_MODE)}/><Select aria-label="Acting as · simulated auth" data={directory.users.map(u=>u.id)} value={actor} onChange={v=>{const selected=v??'sam';sessionStorage.setItem('af-actor',selected);setActor(selected);}}/></div>{['/rules','/people','/runbooks','/proposals'].includes(location.pathname)&&<nav className="secondary-nav" aria-label="Configuration navigation">{[['/rules','Rules'],['/people','Notification preferences'],['/runbooks','Runbooks'],['/proposals','Rule proposals']].map(([path,label])=><NavLink key={path} to={path}>{label}</NavLink>)}</nav>}<main><ErrorBox error={config.error}/>{MOCK_MODE&&<div className="mock-banner">Mock workspace · Simulated events and local actions{admin?' · Admin simulation':''}</div>}<Routes><Route path="/" element={<Overview/>}/><Route path="/alerts" element={<Feed/>}/><Route path="/approvals" element={<Approvals/>}/><Route path="/runbooks" element={MOCK_MODE?<Panel title="Runbook formats"><p>Explore configured runbook formats and simulated actions in the settlement incident.</p><Link to="/a/101">Open settlement runbook →</Link></Panel>:<RunbookCatalogue/>}/><Route path="/a/:id" element={<Detail/>}/><Route path="/rules" element={<Rules/>}/><Route path="/people" element={<People/>}/><Route path="/onboarding" element={MOCK_MODE?<Onboarding/>:<SchemaOnboarding/>}/><Route path="/proposals" element={<ProposalPage/>}/><Route path="/dashboard" element={<Dashboard/>}/><Route path="*" element={<Panel title="Page not found"><Link to="/">Return to live feed</Link></Panel>}/></Routes><details className="connection"><summary>Connection settings</summary><p>Mode changes require restarting Vite. Set <code>VITE_MOCK=1</code> for mock or <code>VITE_MOCK=0</code> for API. Optional <code>VITE_API_BASE=http://localhost:8000</code>; otherwise the development proxy is used. API mode never falls back silently to mock. Set VITE_ALERTS_MODE=api to use real incident, notification and runbook APIs while keeping config screens mocked.</p></details></main></div>{toast&&<div role="status" className="toast">✓ {toast}</div>}</div></Context.Provider>;
}
function Legacy({back,label,children}:{back:string;label:string;children:React.ReactNode}){
 return <div className="cf-legacy"><LegacyLink to={back}>← {label}</LegacyLink>{children}</div>;
}
function LegacyIncident(){
 const {id}=useParams();
 const {actor,directory}=useApp();
 const number=Number(id);
 const detail=useQuery(detailQuery(Number.isSafeInteger(number)&&number>0?number:undefined));
 if(detail.error)return <ErrorBox error={detail.error}/>;
 if(!Number.isSafeInteger(number)||number<=0)return <p>Incident unavailable.</p>;
 if(!detail.data||!directory.users.length)return <p>Loading incident…</p>;
 const member=inTeam(detail.data.team_id,actor,false,directory);
 const approver=directory.teams.some(team=>team.id===detail.data?.team_id&&team.approvers.includes(actor));
 if(!member&&!approver)return <p>Incident unavailable.</p>;
 return <Legacy back={`/a/${id}`} label="Incident"><Detail/></Legacy>;
}
function Feed(){const location=useLocation();const{team,feedEnabled}=useApp();const[status,setStatus]=useState(''),[severity,setSeverity]=useState(new URLSearchParams(location.search).get('severity')??''),[q,setQ]=useState('');const data=useQuery(alertListQuery(team,feedEnabled));const items=filterAlerts(data.data??[],{team,status,severity,q});return <><PageTitle title="Live feed" description="Find what needs attention. Open an incident to understand and act."/><div className="stats"><div><strong>{data.data?.filter(a=>a.status==='open').length??'—'}</strong>Open incidents</div><div><strong>{data.data?.filter(a=>a.status==='acknowledged').length??'—'}</strong>Acknowledged</div><div><strong>{data.data?.reduce((s,a)=>s+a.occurrence_count,0)??'—'}</strong>Grouped occurrences</div></div><div className="filterbar"><TextInput aria-label="Search alerts" placeholder="Search title or rule…" value={q} onChange={e=>setQ(e.target.value)}/><Select aria-label="Status" placeholder="All statuses" clearable data={['open','acknowledged','resolved']} onChange={v=>setStatus(v??'')}/><Select aria-label="Severity" value={severity||null} placeholder="All severities" clearable data={['critical','error','warning','info','unknown']} onChange={v=>setSeverity(v??'')}/></div><ErrorBox error={data.error}/>{feedEnabled&&data.isPending&&<p>Loading incidents…</p>}{!feedEnabled&&<p>Choose an actor with a team to open the live feed.</p>}<div className="list-heading"><span>{items.length} incidents</span><span>Updates arrive automatically</span></div>{items.map(a=><Link className={`incident ${a.color}`} key={a.id} to={`/a/${a.id}`}><div><div className="badges"><Badge color={a.color==='amber'?'yellow':a.color}>{a.severity}</Badge><Badge variant="light" color="gray">{a.status}</Badge><span>{a.team_id}</span></div><h2>{a.title}</h2><p>#{a.id} · {a.rule_id} · First seen {new Date(a.first_seen_at).toLocaleString()}</p></div><div className="counter"><strong>{a.occurrence_count.toLocaleString()}</strong><span>occurrences</span><small title="P0 immediate, P1 high, P2 medium, P3 low. Independent from severity.">Priority: {priorityLabel(a.priority)} {a.priority?`(${a.priority})`:''}</small><small title="Backend ranking score used to order attention. Not a percentage or confidence.">Ranking score: {a.score??'unknown'} →</small></div></Link>)}{!data.isPending&&!data.error&&!items.length&&<Panel title="No matching incidents"><p>Try a different team or clear your filters.</p></Panel>}</>;}
function Detail(){const{id}=useParams();const number=Number(id);const{actor,admin,directory,notice}=useApp();const client=useQueryClient();const[error,setError]=useState(''),[busy,setBusy]=useState(false),[reason,setReason]=useState(''),[proposal,setProposal]=useState(false);const detail=useQuery(detailQuery(number));const notifications=useQuery({queryKey:['detail','notifications',number],queryFn:()=>api.notifications(number)});const[explain,setExplain]=useState<unknown>(null);const a=detail.data;const explainPath=representativeExplainPath(a??{});async function act(action:'ack'|'resolve'){if(!a||!inTeam(a.team_id,actor,admin,directory)){setError('Only team members can change this incident. Use Admin simulation to test other teams.');return;}setBusy(true);setError('');try{await api.transition(number,action,actor);await client.invalidateQueries({queryKey:['alerts']});notice(MOCK_MODE?'Mock incident updated':'Incident updated');}catch(e){setError(String(e));}finally{setBusy(false);}}
 if(detail.error)return <ErrorBox error={detail.error}/>;if(!a)return <p>Loading incident…</p>;
 return <><Link to="/alerts" className="back">← Live feed</Link><PageTitle title={a.title} description={`Incident #${a.id} · ${a.team_id} · ${a.occurrence_count} occurrences`} action={<div className="actions"><Button loading={busy} disabled={a.status!=='open'||!inTeam(a.team_id,actor,admin,directory)} variant="default" onClick={()=>void act('ack')}>Acknowledge</Button><Button loading={busy} disabled={a.status==='resolved'||!inTeam(a.team_id,actor,admin,directory)} onClick={()=>void act('resolve')}>Resolve incident</Button></div>}/><ErrorBox error={error}/><div className="badges"><Badge color="red">{a.severity}</Badge><Badge>{a.status}</Badge></div><p className="muted">Priority: {priorityLabel(a.priority)} ({a.priority??'unknown'}) · Ranking score: {a.score??'unknown'} (attention ordering, not a percentage). P0 = immediate, P1 = high, P2 = medium, P3 = low.</p><div className="detail-grid"><div><Panel title="What happened"><p>{a.body||'No additional description provided.'}</p><dl>{Object.entries(a.fields).map(([k,v])=><div key={k}><dt>{k}</dt><dd>{String(v)}</dd></div>)}</dl>{(ALERTS_MOCK||explainPath)&&<details><summary>Why did I get this?</summary><p>Rule: <strong>{a.rule_id}</strong>. Grouped by <code>{a.alert_key}</code>.</p>{ALERTS_MOCK?<><p>Illustrative trace from the configured rule. Not a measured backend evaluation.</p>{(store.rules.find(r=>r.id===a.rule_id)?.match as {all?:{field:string;value?:unknown}[]})?.all?.map((c,i)=><p key={i}>✓ {c.field} matched {String(c.value)}</p>)}<p>Notifications below show the fixture recipients and channels.</p></>:<Button onClick={async()=>{try{const{request}=await import('./api');setExplain(await request(explainPath!));}catch(e){setError(String(e));}}} disabled={!explainPath}>Load decision trace</Button>}{explain!=null&&<Data value={explain}/>}</details>}</Panel><Panel title="Delivery"><ErrorBox error={notifications.error}/>{notifications.data?.length===0&&<p>No notifications recorded.</p>}{notifications.data?.map(n=><div className="row" key={n.id}><div><strong>{n.channel.toUpperCase()} → {n.recipient}</strong><p>{n.attempts} attempt(s)</p><details><summary>Attempt history</summary><Data value={n.attempt_log}/></details></div><Badge color={n.state==='delivered'?'teal':'orange'}>{n.state}</Badge></div>)}</Panel><Panel title="Activity timeline">{a.timeline.length?a.timeline.map((t,i)=><div className="timeline-item" key={i}><small>{new Date(t.ts).toLocaleString()} · {t.kind}</small><p>{t.text}</p></div>):<p>No activity recorded.</p>}</Panel><Panel title="Recent occurrences"><table><thead><tr><th>Event</th><th>Received</th><th>Fields</th></tr></thead><tbody>{a.occurrences.map(o=><tr key={o.event_id}><td>{o.event_id}</td><td>{new Date(o.seen_at).toLocaleTimeString()}</td><td>{JSON.stringify(o.fields)}</td></tr>)}</tbody></table>{!a.occurrences.length&&<p>No occurrence samples available.</p>}</Panel><Panel title="Reduce noise"><Button variant="default" onClick={()=>setProposal(!proposal)}>Propose fix</Button>{proposal&&<><Textarea label="What should change?" value={reason} onChange={e=>setReason(e.target.value)}/><Button disabled={!reason.trim()} onClick={async()=>{if(!MOCK_MODE){try{const{request}=await import('./api');await request('/proposals',actor,{alert_id:a.id,text:reason});notice(`Proposal sent to the owner of ${a.rule_id}`);setProposal(false);}catch(e){setError(String(e));}return;}const rule=store.rules.find(r=>r.id===a.rule_id);if(!rule){setError('No configurable rule found for this incident');return;}store.proposals.push({id:Date.now(),actor,reason,rule:structuredClone(rule),state:'pending'});persist();record(`${actor} proposed a change to ${rule.id}`,'config',a.id);notice('Mock proposal created. Review in Proposals.');setProposal(false);}}>{MOCK_MODE?'Create mock proposal':'Submit proposal'}</Button></>}</Panel></div><RunbookPanel id={number} teamId={a.team_id}/></div></>;}
export default App;
