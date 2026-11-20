import YAML from 'yaml';
export function conditionValue(op:string,text:string):string|number|boolean|(string|number|boolean)[]{
 if(['in','not_in'].includes(op)){try{const value=YAML.parse(text.trim().startsWith('[')?text:`[${text}]`);return Array.isArray(value)?value:[];}catch{return text.replace(/^\[|\]$/g,'').split(',').map(v=>v.trim()).filter(Boolean);}}
 if(['gt','gte','lt','lte'].includes(op)&&text.trim()!==''&&Number.isFinite(Number(text)))return Number(text);
 return text;
}
export function samplePaths(text:string):string[]{try{const out:string[]=[];const walk=(value:unknown,prefix='')=>{if(value&&typeof value==='object'&&!Array.isArray(value))Object.entries(value).forEach(([key,v])=>{const path=prefix?`${prefix}.${key}`:key;out.push(path);walk(v,path);});};walk(JSON.parse(text));return out;}catch{return [];}}
