import {useEffect,useRef,type ReactNode} from 'react';
export function ScrollReveal({children}:{children:ReactNode}){
 const ref=useRef<HTMLDivElement>(null);
 useEffect(()=>{const node=ref.current;if(!node||!('IntersectionObserver' in window)||window.matchMedia('(prefers-reduced-motion: reduce)').matches)return;
 const observer=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting)){node.classList.remove('reveal-pending');observer.disconnect();}},{threshold:0,rootMargin:'0px 0px -40px 0px'});
 // Keep initial-viewport content visible and avoid trapping keyboard focus offscreen.
 if(node.getBoundingClientRect().top>window.innerHeight)node.classList.add('reveal-pending');
 const reveal=()=>{node.classList.remove('reveal-pending');observer.disconnect();};node.addEventListener('focusin',reveal);observer.observe(node);
 return()=>{observer.disconnect();node.removeEventListener('focusin',reveal);node.classList.remove('reveal-pending');};},[]);
 return <div ref={ref} className="scroll-reveal">{children}</div>;
}
