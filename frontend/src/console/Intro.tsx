import {useEffect, useRef, useState, type CSSProperties} from 'react';
import {useNavigate} from 'react-router-dom';
import {countLabel, Icon} from './ui';

// One example night, start to finish. Each step names the page where you'd do it yourself.
const story = [
  {verb: 'Receive', value: '500', label: 'events', icon: 'sources', page: '/sources', place: 'Sources',
    caption: 'Three systems report the same failure.', how: 'Paste one event. Say what the fields mean.'},
  {verb: 'Read', value: '9', label: 'fields', icon: 'rules', page: '/rules', place: 'Rules',
    caption: 'A format it has never seen. It reads it, you confirm.', how: 'Say what to match and who to page.'},
  {verb: 'Group', value: '1', label: 'incident', icon: 'funnel', page: '/', place: 'Now',
    caption: '500 repeats. One incident.', how: 'Nothing to do. It shows up on Now.'},
  {verb: 'Page', value: '0.24 s', label: 'to Sam', icon: 'phone', page: '/oncall', place: 'On call',
    caption: 'Sam’s phone rings. If nobody answers, Lee is next.', how: 'See who’s on call. Set how you want to be reached.'},
  {verb: 'Fix', value: '6m 40s', label: 'to fixed', icon: 'check', page: '/runbooks', place: 'Runbooks',
    caption: 'One approved retry. Fixed, and it’s all on record.', how: 'Ack it, run the step, close it.'},
] as const;
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
      <h1 id="cf-story-title">500 failures. <em>One phone call.</em></h1>
      <p className="cf-story-sub">Here’s one bad night, start to finish.</p>
      <div className="cf-story-track" data-started={step >= 0} style={{'--fill': shown / last * 100 + '%'} as CSSProperties}>
        <div className="cf-story-line" aria-hidden="true"><i/><b data-ok={step === last}/></div>
        <ol>
          {story.map((item, index) => <li key={item.verb} data-reached={index <= step} data-current={index === step}>
            <button type="button" className="cf-story-node" onClick={() => pick(index)}
              aria-label={`Step ${index + 1} · ${item.verb}`} aria-current={index === step ? 'step' : undefined}>
              <Icon name={item.icon}/>
            </button>
            <span className="cf-story-verb">{item.verb}</span>
            <strong>{index !== 0 ? item.value : step < 0 ? '0'
              : <CountUp key={String(step === 0 && !manual)} to={500} run={step === 0 && !manual}/>}</strong>
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
