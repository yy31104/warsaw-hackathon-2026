import {useEffect} from 'react';
import {useNavigate} from 'react-router-dom';

// A walk past the real pages. It never opens by itself and never covers the page it points at.
export const stops = [
  {path: '/', title: 'Start here', text: 'The top card is the incident that needs someone now. Acknowledge it to stop escalation, then resolve it once it’s fixed.'},
  {path: '/incidents', title: 'Find any incident', text: 'Filter by severity or status. Open a row to see why it fired, who was notified and what happened.'},
  {path: '/sources', title: 'Review a new format', text: 'Events in a format we don’t recognise wait here. Check the suggested fields, test the routing, then publish.'},
  {path: '/rules', title: 'Decide who is notified', text: 'Each rule says what to match and who to notify. Preview it against past events before you publish.'},
  {path: '/runbooks', title: 'Write down the fix', text: 'Steps a responder can follow. Risky steps wait for a second person to approve.'},
  {path: '/oncall', title: 'Who gets called', text: 'See who is on call and who backs them up, and choose how you want to be reached.'},
] as const;

export function Tour({step, setStep}: {step: number; setStep: (step: number | null) => void}) {
  const navigate = useNavigate();
  const stop = stops[step];
  useEffect(() => { navigate(stop.path); }, [navigate, stop.path]);
  const lastStop = step === stops.length - 1;
  return <div className="cf-tour" role="region" aria-label="Tour" aria-live="polite">
    <div className="cf-tour-count">Tour · {step + 1} of {stops.length}</div>
    <strong>{stop.title}</strong>
    <p>{stop.text}</p>
    <footer>
      <button type="button" className="cf-tour-quiet" onClick={() => setStep(null)}>Close</button>
      <span>
        {step > 0 && <button type="button" className="cf-tour-quiet" onClick={() => setStep(step - 1)}>Back</button>}
        <button type="button" onClick={() => setStep(lastStop ? null : step + 1)}>{lastStop ? 'Done' : 'Next stop →'}</button>
      </span>
    </footer>
  </div>;
}
