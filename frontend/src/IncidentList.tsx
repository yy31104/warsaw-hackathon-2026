import {useState} from 'react';
import {Button} from '@mantine/core';
import {useQueryClient} from '@tanstack/react-query';
import {Link} from 'react-router-dom';
import {api} from './api';
import {useApp,ErrorBox} from './App';
import {inTeam,priorityLabel} from './permissions';
import {orderAlerts} from './ActiveAlerts';
import {tint} from './AlertField';
import {AnimatedValue} from './StatusOverview';
import type {Alert} from './model';
export function IncidentList({items}:{items:Alert[]}){
 const {actor,admin,notice}=useApp();const client=useQueryClient();const [busy,setBusy]=useState<number|null>(null),[error,setError]=useState('');
 async function act(a:Alert,kind:'ack'|'resolve'){
  if(busy!==null||!inTeam(a.team_id,actor,admin))return;
  setBusy(a.id);setError('');try{await api.transition(a.id,kind,actor);await client.invalidateQueries({queryKey:['alerts']});notice(`Incident #${a.id} ${kind==='ack'?'acknowledged':'resolved'}`);}catch(e){setError(String(e));}finally{setBusy(null);}
 }
 return <section className="incident-console" aria-label="Incident operations"><ErrorBox error={error}/>{orderAlerts(items).map(a=><article key={a.id} className="incident-console-row" style={{borderInlineStartColor:tint(a)}}>
 <div><div className="incident-meta"><span style={{color:tint(a)}}>{a.severity}</span><span>{priorityLabel(a.priority)} priority</span><span>{a.status}</span><span>#{a.id}</span></div><Link className="incident-title" to={`/a/${a.id}`}>{a.title}</Link><p className="muted">Team: {a.team_id||'‹missing: team›'} · Rule: {a.rule_id}</p><div className="incident-meta"><span>First seen <time>{new Date(a.first_seen_at).toLocaleString()}</time></span><span>Latest <time>{new Date(a.last_seen_at).toLocaleString()}</time></span></div></div>
 <div className="incident-count"><AnimatedValue value={a.occurrence_count}/><small>occurrences</small></div>
 <div className="incident-actions"><Button size="xs" variant="subtle" disabled={busy!==null||a.status!=='open'||!inTeam(a.team_id,actor,admin)} onClick={()=>void act(a,'ack')}>Acknowledge</Button><Button size="xs" variant="subtle" disabled={busy!==null||a.status==='resolved'||!inTeam(a.team_id,actor,admin)} onClick={()=>void act(a,'resolve')}>Resolve</Button>{a.runbook_id&&<Link to={`/a/${a.id}#runbook`}>Runbook & approvals ↗</Link>}<Link to={`/a/${a.id}#propose-fix`}>Propose fix ↗</Link><Link to={`/a/${a.id}`}>Delivery & history ↗</Link></div>
 </article>)}</section>;
}
