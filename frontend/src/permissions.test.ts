import {expect,it,vi} from 'vitest';
vi.mock('./store',()=>({directory:{teams:[{id:'settlements',members:['sam','lee'],approvers:['lee']}]}}));
import {ownsRule,inTeam,priorityLabel} from './permissions';
import type {Directory} from './types';
it('does not grant rule edits to members of a team without approvers',()=>{
 const directory:Directory={teams:[{id:'monitoring',members:['sam']}],users:[],default_preferences:[]};
 expect(ownsRule('team:monitoring','sam',false,directory)).toBe(false);
 expect(inTeam('monitoring','sam',false,directory)).toBe(true);
});
it('limits user edits to personal ownership and team approvers',()=>{expect(ownsRule('user:anna','sam',false)).toBe(false);expect(ownsRule('user:anna','anna',false)).toBe(true);expect(ownsRule('team:settlements','sam',false)).toBe(false);expect(ownsRule('team:settlements','lee',false)).toBe(true);expect(ownsRule('user:anna','sam',true)).toBe(true);});
it('allows incident actions for members and mock administrators',()=>{expect(inTeam('settlements','sam',false)).toBe(true);expect(inTeam('settlements','anna',false)).toBe(false);expect(inTeam('settlements','anna',true)).toBe(true);expect(priorityLabel('P1')).toBe('High');expect(priorityLabel(null)).toBe('Unranked');});
