import {expect,it,vi} from 'vitest';
vi.mock('./store',()=>({directory:{teams:[],users:[{id:'anna',contacts:{sms:'demo-number'}}],default_preferences:[{min_severity:'info',channels:['ui']}]},store:{preferences:{}}}));
import {flatten,compareRule,trace} from './verification';
import {evaluate} from './ruleLogic';
import type {Rule} from './types';
const rule:Rule={id:'fx',owner:'user:anna',version:1,enabled:true,origin:'ui',match:{all:[{field:'$schema',op:'glob',value:'trade.rejected.*'},{field:'payload.desk',op:'eq',value:'fx'}]},route:[{to:'user:anna',channels:['sms']}]};
it('preserves typed arbitrary nested paths and arrays',()=>{expect(flatten({any:{count:12},items:[{id:4}]})).toEqual({'any.count':12,items:[{id:4}],'items.0.id':4});});
it('detects a lost notification and explains deciding conditions',()=>{const fields={'$schema':'trade.rejected.v2','payload.desk':'fx'};const draft={...rule,match:{field:'payload.desk',op:'eq',value:'rates'}};expect(compareRule(rule,draft,[{name:'fx trade',fields}])[0]).toMatchObject({before:['anna/sms'],after:[],lost:true});expect(trace(rule.match,fields).every(c=>c.matched)).toBe(true);expect(evaluate(rule.match,{'$schema':'trade.rejected.v2','payload.desk':'rates'})).toBe(false);});
