import {useEffect, useRef, useState, type CSSProperties} from 'react';
import {useNavigate} from 'react-router-dom';
import {countLabel, Icon} from './ui';

// The whole product in five steps, told with one example incident. Each step names the page where you'd do it.
const story = [
  {verb: 'Receive', value: '3 sources', label: 'API, queue, logs', icon: 'sources', page: '/health', place: 'Health',
    caption: 'Events arrive from APIs, message queues and log files.', how: 'See what’s arriving and how fast it’s processed.'},
  {verb: 'Verify', value: 'Mapped', label: 'a person confirms', icon: 'rules', page: '/sources', place: 'Sources',
    caption: 'New formats are mapped, and a person confirms the fields before anything is routed.', how: 'Check the suggested fields, then publish.'},
  {verb: 'Decide', value: '500', label: 'events → 1 incident', icon: 'funnel', page: '/rules', place: 'Rules',
    caption: 'Rules decide what matters. 500 repeats become one incident; one critical event is enough to page.', how: 'Set what to match and who to notify.'},
  {verb: 'Notify', value: 'Phone + SMS', label: 'to the on-call', icon: 'phone', page: '/oncall', place: 'On call',
    caption: 'Sam is on call and gets a call and a text. If nobody acknowledges, Lee is next.', how: 'Check who is on call and how they’re reached.'},
  {verb: 'Respond', value: 'Approved', label: 'before risky steps', icon: 'shield', page: '/', place: 'Now',
    caption: 'Sam follows the runbook. A risky retry waits for Lee’s approval, and every step is logged.', how: 'Acknowledge, run the step, resolve.'},
] as const;
const counted = 2; // the Decide step counts its events up when it plays
const last = story.length - 1;
const startMs = 350, stepMs = 1900, holdMs = 3600, countMs = 900;

export function readSession(key: string) {
  try { return sessionStorage.getItem(key); } catch { return null; }
}

export function writeSession(key: string, value: string) {
  try { sessionStorage.setItem(key, value); } catch { /* continue with in-memory state */ }
}

function CountUp({to, run}: {to: number; run: boolean}) {
  const [value, setValue] = useState(run ? 0 : to);
  useEffect(() => {
    if (!run) return;
    let frame = 0;
    const start = performance.now();
    const tick = (time: number) => {
      const progress = Math.min(1, (time - start) / countMs);
      setValue(Math.round(to * (1 - (1 - progress) ** 3)));
      if (progress < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [run, to]);
  return <>{countLabel(value)}</>;
}

export function Intro({close, tour}: {close: () => void; tour: () => void}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const navigate = useNavigate();
  const [reduced] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [step, setStep] = useState(reduced ? last : -1);
  // Autoplay until someone picks a step; after that they set the pace and nothing closes on its own.
  const [manual, setManual] = useState(reduced);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);
  useEffect(() => {
    if (manual) return;
    const timer = window.setTimeout(step === last ? close : () => setStep(step + 1),
      step < 0 ? startMs : step === last ? holdMs : stepMs);
    return () => window.clearTimeout(timer);
  }, [close, manual, step]);

  const shown = Math.max(0, step);
  const current = story[shown];
  const pick = (index: number) => { setManual(true); setStep(index); };
  const go = (page: string) => { close(); navigate(page); };
  const done = manual || step === last;

  return <dialog ref={dialog} className="cf-intro" aria-labelledby="cf-story-title" onCancel={event => {
    event.preventDefault(); close();
  }}>
    <div className="cf-story">
      <div className="cf-story-brand"><span className="cf-story-logo"><Icon name="funnel"/></span>Alert Funnel <span>Example</span></div>
      <h1 id="cf-story-title">Know what matters. <em>Know what to do.</em></h1>
      <p className="cf-story-sub">Events from different systems, in one place. See what matters, reach the right person, and respond safely.</p>
      <div className="cf-story-track" data-started={step >= 0} style={{'--fill': shown / last * 100 + '%'} as CSSProperties}>
        <div className="cf-story-line" aria-hidden="true"><i/><b data-ok={step === last}/></div>
        <ol>
          {story.map((item, index) => <li key={item.verb} data-reached={index <= step} data-current={index === step}>
            <button type="button" className="cf-story-node" onClick={() => pick(index)}
              aria-label={`Step ${index + 1} · ${item.verb}`} aria-current={index === step ? 'step' : undefined}>
              <Icon name={item.icon}/>
            </button>
            <span className="cf-story-verb">{item.verb}</span>
            <strong>{index !== counted ? item.value : step < counted ? '0'
              : <CountUp key={String(step === counted && !manual)} to={500} run={step === counted && !manual}/>}</strong>
            <small>{item.label}</small>
          </li>)}
        </ol>
      </div>
      <div className="cf-story-copy" aria-live="polite">
        <p className="cf-story-caption">{current.caption}</p>
        <p className="cf-story-how">
          <span className="cf-story-step">You</span>{current.how}
          <button type="button" className="cf-story-go" onClick={() => go(current.page)}>Open {current.place} →</button>
        </p>
      </div>
      <div className="cf-story-footer">
        <span className="cf-story-count">{shown + 1} / {story.length}</span>
        <div className="cf-story-bar" aria-hidden="true"><i style={{width: (shown + 1) / story.length * 100 + '%'}}/></div>
        <div className="cf-actions">
          {manual && step < last && <button type="button" className="cf-btn cf-story-next" onClick={() => pick(step + 1)}>Next</button>}
          {done && <button type="button" className="cf-btn cf-story-next" onClick={() => { close(); tour(); }}>Show me around</button>}
          <button type="button" className="cf-btn primary" autoFocus onClick={close}>{done ? 'Open console' : 'Skip'}</button>
        </div>
      </div>
    </div>
  </dialog>;
}
