import YAML from 'yaml';
import rulesText from '../../config/rules.yaml?raw';
import teamsText from '../../config/teams.yaml?raw';
import type {Directory,Rule,Preference} from './types';
export const directory=YAML.parse(teamsText) as Directory;
const initial=(YAML.parse(rulesText).rules as Rule[]).map(r=>({...r,origin:'file' as const,version:1,enabled:true}));
export type Proposal={id:number;rule:Rule;actor:string;reason:string;state:string};
function read<T>(key:string,fallback:T):T{try{return JSON.parse(localStorage.getItem(key)??'null')??fallback;}catch{return fallback;}}
export const store={rules:read<Rule[]>('af-rules',initial),preferences:read<Record<string,Preference[]>>('af-prefs',{}),proposals:read<Proposal[]>('af-proposals',[]),schemaSaved:read<boolean>('af-schema',false)};
export function persist(){localStorage.setItem('af-rules',JSON.stringify(store.rules));localStorage.setItem('af-prefs',JSON.stringify(store.preferences));localStorage.setItem('af-proposals',JSON.stringify(store.proposals));localStorage.setItem('af-schema',JSON.stringify(store.schemaSaved));}
