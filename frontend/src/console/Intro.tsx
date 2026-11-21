import {useEffect, useRef, useState} from 'react';
import {Icon} from './ui';

const story = [
  {value: '500', label: 'events in', icon: 'sources', caption: 'Three systems report the same settlement failure.'},
  {value: '9 fields', label: 'understood', icon: 'rules', caption: 'New format? We read it. You confirm what it means.'},
  {value: '1', label: 'incident', icon: 'funnel', caption: '500 repeats become one incident.'},
  {value: '0.24 s', label: 'to reach Sam', icon: 'phone', caption: 'Sam’s phone rings. Lee is next if nobody answers.'},
  {value: '6m 40s', label: 'to fix', icon: 'check', caption: 'One approved retry. Fixed, and every step is on record.'},
] as const;

export function readSession(key: string) {
  try { return sessionStorage.getItem(key); } catch { return null; }
}

export function writeSession(key: string, value: string) {
  try { sessionStorage.setItem(key, value); } catch { /* continue with in-memory state */ }
}

export function Intro({close}: {close: () => void}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [reduced] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [step, setStep] = useState(reduced ? story.length - 1 : 0);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);
  useEffect(() => {
    if (reduced) return;
    const timer = window.setTimeout(step === story.length - 1 ? close : () => setStep(step + 1), 1200);
    return () => window.clearTimeout(timer);
  }, [close, reduced, step]);
  return <dialog ref={dialog} className="cf-intro" aria-labelledby="cf-story-title" onCancel={event => {
    event.preventDefault(); close();
  }}>
    <div className="cf-story">
      <div className="cf-story-brand"><Icon name="funnel"/> Alert Funnel <span>Example</span></div>
      <h1 id="cf-story-title">500 failures. <em>One phone call.</em></h1>
      <ol className="cf-story-track">
        {story.map((item, index) => <li key={item.label} data-reached={index <= step}>
          <div className="cf-story-node"><Icon name={item.icon}/></div>
          <strong>{item.value}</strong><small>{item.label}</small>
        </li>)}
      </ol>
      <p className="cf-story-caption">{story[step].caption}</p>
      <div className="cf-story-footer"><progress value={step + 1} max={story.length} aria-label="Example progress"/>
        <button className="cf-btn primary" onClick={close}>{reduced ? 'Open console' : 'Skip'}</button>
      </div>
    </div>
  </dialog>;
}
