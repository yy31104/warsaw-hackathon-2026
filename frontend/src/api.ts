import detailFixture from '../../fixtures/api/alert_detail.json';
import notifications from '../../fixtures/api/notifications.json';
import {session,startReplay,applyStream,mockTransition,mockRunbook,resetScenario,changed,emitMockOccurrence} from './session';
import runbook from '../../fixtures/api/runbook.json';
import replay from '../../fixtures/api/stream.ndjson?raw';
import {type Alert,type StreamMessage} from './model';
export const MOCK_MODE=import.meta.env.VITE_MOCK==='1';
const base=(import.meta.env.VITE_API_BASE??'').replace(/\/$/,'');

export class RequestError extends Error {
 constructor(readonly status:number,message:string,readonly detail?:unknown){super(`${status} ${message}`);}
}

export async function request<T>(path:string,actor?:string,body?:unknown,method?:'GET'|'POST'|'PUT'):Promise<T>{
 const assisted=['/schemas/suggest','/rules/draft','/runbooks/structure'].includes(path);
 const response=await fetch(`${base}/v1${path}`,{method:method??(body===undefined?'GET':'POST'),headers:{...(body===undefined?{}:{'Content-Type':'application/json'}),...(actor?{'X-User':actor}:{})},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(assisted?45000:10000)});
 if(!response.ok){
  const result=await response.json().catch(()=>null);
  const detail=result?.detail;
  const message=typeof detail==='string'?detail:detail?.message;
  throw new RequestError(response.status,message??`${response.statusText} — ${path}`,detail);
 }
 return response.status===204?undefined as T:response.json();
}
export const api={
 alerts:async(team:string,status?:string)=>{
  if(ALERTS_MOCK)return structuredClone(session.alerts.filter(alert=>(!team||alert.team_id===team)&&(!status||alert.status===status)));
  const search=new URLSearchParams();
  if(team)search.set('team',team);
  if(status)search.set('status',status);
  return (await request<{items:Alert[]}>(`/alerts${search.size?`?${search}`:''}`)).items;
 },
 detail:async(id:number)=>{if(!ALERTS_MOCK)return request<typeof detailFixture>(`/alerts/${id}`);const item=session.alerts.find(a=>a.id===id);if(!item)throw new Error('Incident not found');return {...structuredClone(item),occurrences:id===101?detailFixture.occurrences:[],timeline:structuredClone(session.timelines[id]??[])};},
 notifications:async(id:number)=>ALERTS_MOCK?(session.notifications.filter(n=>n.alert_id===id)):(await request<typeof notifications>(`/alerts/${id}/notifications`)).items,
 runbook:async(id:number)=>{
  if(ALERTS_MOCK)return id===101?mockRunbook():null;
  try{return await request<typeof runbook>(`/alerts/${id}/runbook`);}
  catch(error){if(error instanceof RequestError&&error.status===404)return null;throw error;}
 },
 transition:async(id:number,action:'ack'|'resolve',actor:string)=>{if(!ALERTS_MOCK)return request(`/alerts/${id}/${action}`,actor,{});mockTransition(id,action,actor);},
};
export function alertListQuery(team:string,enabled=true,status?:string){
 return {queryKey:['alerts','list',team,status],queryFn:()=>api.alerts(team,status),enabled};
}
let replayIndex=0;
export function subscribe(onMessage:(message:StreamMessage)=>void,onState:(state:string)=>void,team=''){
 if(MOCK_MODE){startReplay();onState('Simulated SSE replay');const rows=replay.trim().split('\n').map(row=>JSON.parse(row) as StreamMessage);let lastTick=0;const timer=setInterval(()=>{if(session.replayFinished&&session.simulationRunning){onState('Simulated SSE · streaming');if(Date.now()-lastTick>=3000){lastTick=Date.now();const event=emitMockOccurrence();if(event)onMessage(event);}return;}if(!session.replayRunning){onState(session.replayFinished?'Simulated SSE · replay complete':'Simulated SSE · paused');return;}if(Date.now()-lastTick<session.replayDelay)return;lastTick=Date.now();onState('Simulated SSE replay');if(replayIndex>=rows.length){session.replayRunning=false;session.replayFinished=true;applyStream({type:'alert.updated',data:{id:101,occurrence_count:500}});onMessage({type:'alert.updated',data:{id:101,occurrence_count:500}});onState('Simulated SSE · replay complete');return;}const message=rows[replayIndex++];applyStream(message);onMessage(message);},100);return()=>clearInterval(timer);}
 onState('Connecting');
 const stream=new EventSource(`${base}/v1/stream${team?`?team=${encodeURIComponent(team)}`:''}`);
 stream.onopen=()=>onState('Live');
 stream.onerror=()=>onState('Reconnecting');
 for(const type of ['alert.created','alert.updated','notification.updated','action.updated','schema.issue','config.changed','stats.tick']){
  stream.addEventListener(type,event=>{
   try{onMessage({type,data:JSON.parse((event as MessageEvent).data)});}
   catch{onState('Invalid stream payload');}
  });
 }
 return()=>stream.close();
}

export function replayDemo(){replayIndex=0;resetScenario();}
export function toggleReplay(){session.replayRunning=!session.replayRunning;changed();}
export function setReplaySpeed(delay:number){session.replayDelay=delay;changed();}
export const ALERTS_MOCK=MOCK_MODE&&import.meta.env.VITE_ALERTS_MODE!=='api';
