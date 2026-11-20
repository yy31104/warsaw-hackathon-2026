import {expect,it} from 'vitest';
import fixture from '../../fixtures/api/alerts.json';
import {filterAlerts,mergeAlert} from './model';
it('applies partial SSE changes without losing incident fields or changing other incidents',()=>{
 const result=mergeAlert(fixture.items,{type:'alert.updated',data:{id:101,occurrence_count:52}});
 expect(result[0].occurrence_count).toBe(52);expect(result[0].title).toBe(fixture.items[0].title);expect(result[1]).toBe(fixture.items[1]);expect(fixture.items[0].occurrence_count).toBe(500);
});
it('combines filters and ignores notification IDs in incident updates',()=>{
 expect(filterAlerts(fixture.items,{team:'rates',status:'open',severity:'warning',q:'eur'}).map(a=>a.id)).toEqual([102]);
 expect(mergeAlert(fixture.items,{type:'notification.updated',data:{id:101}})).toBe(fixture.items);
});
