import {expect,it} from 'vitest';
import fixture from '../../fixtures/api/alerts.json';
import {filterAlerts,mergeAlert} from './model';
it('opens the same critical queue counted on the overview, including acknowledged alerts',()=>{
 const sample=fixture.items[0];
 const rows=['open','acknowledged','resolved'].map((status,id)=>({...sample,id,status,severity:'critical'}));
 rows.push({...sample,id:3,status:'open',severity:'warning'});
 expect(filterAlerts(rows,{team:sample.team_id,status:'active',severity:'critical',q:''}).map(a=>a.id)).toEqual([0,1]);
});
it('applies partial SSE changes without losing incident fields or changing other incidents',()=>{
 const result=mergeAlert(fixture.items,{type:'alert.updated',data:{id:101,occurrence_count:52}});
 expect(result[0].occurrence_count).toBe(52);expect(result[0].title).toBe(fixture.items[0].title);expect(result[1]).toBe(fixture.items[1]);expect(fixture.items[0].occurrence_count).toBe(500);
});
it('combines filters and ignores notification IDs in incident updates',()=>{
 expect(filterAlerts(fixture.items,{team:'rates',status:'open',severity:'warning',q:'eur'}).map(a=>a.id)).toEqual([102]);
 expect(mergeAlert(fixture.items,{type:'notification.updated',data:{id:101}})).toBe(fixture.items);
});
