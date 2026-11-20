import {expect,it,vi} from 'vitest';
vi.mock('./store',()=>({directory:{teams:[{id:'settlements',members:['sam','lee'],approvers:['lee']}]}}));
import {ownsRule,inTeam,priorityLabel} from './permissions';
it('limits user edits to personal ownership and team approvers',()=>{expect(ownsRule('user:anna','sam',false)).toBe(false);expect(ownsRule('user:anna','anna',false)).toBe(true);expect(ownsRule('team:settlements','sam',false)).toBe(false);expect(ownsRule('team:settlements','lee',false)).toBe(true);expect(ownsRule('user:anna','sam',true)).toBe(true);});
it('allows incident actions for members and mock administrators',()=>{expect(inTeam('settlements','sam',false)).toBe(true);expect(inTeam('settlements','anna',false)).toBe(false);expect(inTeam('settlements','anna',true)).toBe(true);expect(priorityLabel('P1')).toBe('High');expect(priorityLabel(null)).toBe('Unranked');});
