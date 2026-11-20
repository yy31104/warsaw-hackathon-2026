import {expect,it,vi} from 'vitest';
vi.mock('./store',()=>({directory:{teams:[]}}));
import {orderAlerts} from './ActiveAlerts';
import fixture from '../../fixtures/api/alerts.json';
it('puts severe failures ahead of high-score warnings without mutating input',()=>{const items=structuredClone(fixture.items);items[1].score=999;items.reverse();const before=items.map(a=>a.id);expect(orderAlerts(items)[0].severity).toBe('error');expect(items.map(a=>a.id)).toEqual(before);});
