import {directory} from './store';
import type {Directory} from './types';
export function ownsRule(owner:string,actor:string,admin:boolean,activeDirectory:Directory=directory){return admin||owner===`user:${actor}`||!!activeDirectory.teams.find(t=>owner===`team:${t.id}`)?.approvers?.includes(actor);}
export function inTeam(team:string,actor:string,admin:boolean,activeDirectory:Directory=directory){return admin||!!activeDirectory.teams.find(t=>t.id===team)?.members.includes(actor);}
export function priorityLabel(value:string|null){return ({P0:'Immediate',P1:'High',P2:'Medium',P3:'Low'} as Record<string,string>)[value??'']??'Unranked';}
