import {useQuery} from '@tanstack/react-query';
import {api,ALERTS_MOCK} from './api';
export function NotificationPreview({id}:{id:number}){
 const query=useQuery({queryKey:['detail','notifications',id],queryFn:()=>api.notifications(id)});
 return <small>{ALERTS_MOCK?'Simulated delivery: ': 'Delivery: '}{query.isPending?'loading…':query.isError?'unavailable':query.data?.length?query.data.map(n=>`${n.channel} → ${n.recipient}: ${n.state}`).join(' · '):'no notifications recorded'}</small>;
}
