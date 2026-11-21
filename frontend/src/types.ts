export type {Alert,StreamMessage} from './model';
export type Condition={field:string;op:string;value?:string|number|boolean};
export type Group={all?:Node[];any?:Node[];not?:Node};
export type Node=Condition|Group;
export type Rule={id:string;owner:string;origin:'file'|'ui';version:number;enabled:boolean;match:Node;route:{to:string;channels:string[]|'preferences'}[];present?:{title?:string;body?:string;fields?:string[];color?:string};group?:{mode:string;cooldown?:string;every?:string};plain_english?:string};
export type Preference={when?:string;min_severity:string;channels:string[]};
export type Directory = {
  fallback_team?: string;
  default_preferences: Preference[];
  teams: {id: string; members: string[]; approvers?: string[]; oncall?: {primary: string}; required_channels?: Record<string, string[]>}[];
  users: {id: string; name: string; timezone: string; contacts?: Record<string, string>; preferences?: Preference[]}[];
};
