import {describe,it,expect} from 'vitest';
import {conditionValue,samplePaths} from './editorValues';
import {tint} from './AlertField';
describe('rule editor API values',()=>{it('sends membership values as arrays',()=>{expect(conditionValue('in','[TIMEOUT, REJECTED]')).toEqual(['TIMEOUT','REJECTED']);expect(conditionValue('not_in','["TIMEOUT", "REJECTED"]')).toEqual(['TIMEOUT','REJECTED']);});it('discovers unfamiliar nested fields',()=>{expect(samplePaths('{"novel":{"duration":37}}')).toContain('novel.duration');});it('uses configured colours independently of severity',()=>{expect(tint({color:'blue'})).not.toBe(tint({color:'amber'}));});});
