import {useEffect,useRef,useState,type CSSProperties} from 'react';
import {Link} from 'react-router-dom';
import {Button} from '@mantine/core';
import type {Alert} from './model';
import {orderAlerts} from './ActiveAlerts';
export function AnimatedValue({value}:{value:number|null}){const[display,setDisplay]=useState(value);const previous=useRef(value);useEffect(()=>{const start=previous.current;previous.current=value;if(value==null||start==null||window.matchMedia('(prefers-reduced-motion: reduce)').matches){setDisplay(value);return;}const begin=performance.now();let frame=0;const tick=(now:number)=>{const t=Math.min((now-begin)/450,1);setDisplay(Math.round(start+(value-start)*(1-Math.pow(1-t,3))));if(t<1)frame=requestAnimationFrame(tick);};frame=requestAnimationFrame(tick);return()=>cancelAnimationFrame(frame);},[value]);return <span className="animated-value" aria-label={value==null?'Unknown':String(value)}>{display??'—'}</span>;}
export function StatusOverview({alerts,pending,loading}:{alerts:Alert[];pending:number|null;risks:number|null;loading:boolean}){
 const focus=orderAlerts(alerts.filter(alert=>alert.status!=='resolved'))[0];
 const [selected,setSelected]=useState<number|null>(null);
 const closeTimer=useRef<ReturnType<typeof setTimeout>|null>(null);
 const cancelClose=()=>{if(closeTimer.current)clearTimeout(closeTimer.current);};
 useEffect(()=>()=>{if(closeTimer.current)clearTimeout(closeTimer.current);},[]);
 const groups=[
  {label:'Errors & critical',color:'var(--motion-pink)',items:alerts.filter(a=>['error','critical'].includes(a.severity))},
  {label:'Warnings',color:'var(--motion-orange)',items:alerts.filter(a=>a.severity==='warning')},
  {label:'Other alerts',color:'var(--motion-purple)',items:alerts.filter(a=>!['error','critical','warning'].includes(a.severity))}
 ];
 const circumference=2*Math.PI*51;let offset=0;
 const before=selected===null?0:groups.slice(0,selected).reduce((sum,g)=>sum+g.items.length,0);
 const angle=selected===null?0:((before+groups[selected].items.length/2)/Math.max(alerts.length,1))*Math.PI*2-Math.PI/2;
 const anchorX=Math.cos(angle),anchorY=Math.sin(angle);
 const popupStyle={'--anchor-x':`calc(50% + clamp(240px, 24vw, 320px) * ${anchorX*.43})`,'--anchor-y':`calc(clamp(240px, 24vw, 320px) * ${.5+anchorY*.43})`,'--popup-shift-x':anchorX>0.2?'0%':anchorX<-.2?'-100%':'-50%','--popup-shift-y':anchorY>0.2?'0%':anchorY<-.2?'-100%':'-50%'} as CSSProperties;
 const show=(i:number)=>{cancelClose();setSelected(i);};
 return <section className="orbit-overview" aria-label="Operational status overview" onKeyDown={e=>{if(e.key==='Escape'){setSelected(null);}}}>
 <div className="interactive-orbit-area" onMouseEnter={cancelClose} onMouseLeave={()=>{cancelClose();closeTimer.current=setTimeout(()=>setSelected(null),250);}} onBlur={e=>{if(!e.currentTarget.contains(e.relatedTarget))setSelected(null);}}>
 <div className="interactive-orbit"><svg viewBox="0 0 120 120" aria-label="Active alerts by severity"><circle cx="60" cy="60" r="51" fill="none" stroke="var(--line)" strokeWidth="5"/>{groups.map((group,i)=>{const length=alerts.length?group.items.length/alerts.length*circumference:0;const current=offset;offset+=length;return group.items.length>0?<circle key={group.label} role="button" tabIndex={0} aria-label={`${group.label}: ${group.items.length}. Show alerts`} aria-expanded={selected===i} aria-controls="orbit-alert-popup" onMouseEnter={()=>show(i)} onFocus={()=>show(i)} onClick={()=>show(i)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();show(i);}}} className={`orbit-segment ${selected===i?'is-selected':''}`} cx="60" cy="60" r="51" fill="none" stroke={group.color} strokeWidth={selected===i?7:5} strokeDasharray={`${Math.max(0,length-3)} ${circumference}`} strokeDashoffset={-current} transform="rotate(-90 60 60)"/>:null;})}</svg><div className="orbit-center"><strong><AnimatedValue value={loading?null:alerts.length}/></strong><h2>Active alerts</h2></div></div>
 <div className="orbit-legend">{groups.map((group,i)=><button key={group.label} aria-expanded={selected===i} aria-controls="orbit-alert-popup" onMouseEnter={()=>show(i)} onFocus={()=>show(i)} onClick={()=>show(i)}><i style={{background:group.color}}/>{group.items.length} {group.label.toLowerCase()}</button>)}</div>
 <p className="orbit-hint">Hover or select a colour to investigate</p>
 {selected!==null&&<div className="orbit-alert-popup" style={popupStyle} data-group={selected} id="orbit-alert-popup" role="region" aria-label={groups[selected].label}><div className="orbit-popup-heading"><button aria-label="Close alert list" onClick={()=>setSelected(null)}>×</button></div><div className="orbit-popup-items">{groups[selected].items.map(alert=><Link key={alert.id} to={`/a/${alert.id}`} className="orbit-alert-item" aria-label={`Investigate ${alert.severity}: ${alert.title}`}><span className="orbit-alert-severity">{alert.severity}</span><span>{alert.team_id}</span><span>{alert.occurrence_count.toLocaleString()} occurrences <span>Investigate ↗</span></span></Link>)}{!groups[selected].items.length&&<p>No alerts in this group.</p>}</div></div>}
 </div>
 <div className="attention-spotlight">{loading?<p>Finding the highest-priority alert…</p>:focus?<Link to={`/a/${focus.id}`} className="attention-alert"><span className="attention-caption">Needs your attention <span className={`attention-severity severity-${focus.severity}`}>{focus.severity}</span></span><strong>{focus.title}</strong><span className="attention-meta"><span>{focus.team_id} · {focus.occurrence_count.toLocaleString()} occurrences</span><span>Investigate alert ↗</span></span></Link>:<p>No active alerts need attention.</p>}</div>
 <Button component={Link} className="approval-status-button" to="/approvals" variant="filled" size="md" rightSection={<span aria-hidden="true">→</span>}>Pending approvals<span className="approval-button-count"><AnimatedValue value={pending}/></span></Button>
 </section>;
}
