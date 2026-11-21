import {useEffect} from 'react';
import {useNavigate} from 'react-router-dom';

// A walk past the real pages. It never opens by itself and never covers the page it points at.
export const stops = [
  {path: '/', title: 'Start here', text: 'The red card is the one that needs you. Acknowledge it and the paging stops.'},
  {path: '/incidents', title: 'See everything', text: 'Tap a colour to filter by how bad it is. Open a row for the whole story.'},
  {path: '/sources', title: 'Hook up a system', text: 'Paste one sample event. It guesses the fields, you say what they mean.'},
  {path: '/rules', title: 'Decide who gets paged', text: 'Say what to match and who to call. Test it on real history before you save.'},
  {path: '/runbooks', title: 'Write down the fix', text: 'Steps anyone can follow. Risky ones wait for a second person to approve.'},
  {path: '/oncall', title: 'Who picks up', text: 'See who’s on call and who backs them up. Then set when you want a call, a text or just an email.'},
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
