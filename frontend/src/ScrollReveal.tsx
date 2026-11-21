import {useEffect,useRef,type ReactNode} from 'react';
export function ScrollReveal({children}:{children:ReactNode}){
 const ref=useRef<HTMLDivElement>(null);
 useEffect(()=>{const root=ref.current;if(!root||!('IntersectionObserver' in window)||window.matchMedia('(prefers-reduced-motion: reduce)').matches)return;
 const nodes=[...root.querySelectorAll<HTMLElement>('.scene-intro,.scene-source,.scene-core,.scene-group,.scene-approval-button,.page-title,.demo-session,.chart-heading,.trend-metric-switch,.live-trend svg,.multi-alert-trends,.chart-scrubber,.activity-heading,.activity-tools,.activity-events,.overview-insights')];
 const reveal=(node:Element)=>{node.classList.remove('reveal-pending');observer.unobserve(node);};
 const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting)reveal(entry.target);}),{threshold:0,rootMargin:'0px 0px -24px 0px'});
 nodes.forEach(node=>{node.classList.add('reveal-part');if(node.getBoundingClientRect().top>=window.innerHeight-24)node.classList.add('reveal-pending');observer.observe(node);});
 const focus=(event:FocusEvent)=>{nodes.filter(node=>node.contains(event.target as Node)).forEach(reveal);};root.addEventListener('focusin',focus);
 return()=>{observer.disconnect();root.removeEventListener('focusin',focus);nodes.forEach(node=>node.classList.remove('reveal-part','reveal-pending'));};},[]);
 return <div ref={ref} className="reveal-container">{children}</div>;
}
