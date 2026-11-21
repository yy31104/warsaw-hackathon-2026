import fixture from '../../fixtures/api/alerts.json';
export type Alert = (typeof fixture.items)[number];
export type StreamMessage = {type:string;data:Omit<Partial<Alert>,'id'> & {id?:number|string;alert_id?:number;ts?:string}};
export function mergeAlert(items:Alert[], message:StreamMessage):Alert[] {
  if (!['alert.created','alert.updated'].includes(message.type)) return items;
  return items.map(item=>item.id===message.data.id ? {...item,...message.data,id:item.id} as Alert : item);
}
export function filterAlerts(items:Alert[], filters:{team:string;status:string;severity:string;q:string}) {
 const search=filters.q.trim().toLowerCase();
 return items.filter(a=>{
  const inStatus=filters.status==='active' ? a.status!=='resolved' : !filters.status||a.status===filters.status;
  return inStatus && (!filters.team||a.team_id===filters.team) && (!filters.severity||a.severity===filters.severity)
   && `${a.title} ${a.body} ${a.rule_id}`.toLowerCase().includes(search);
 });
}
