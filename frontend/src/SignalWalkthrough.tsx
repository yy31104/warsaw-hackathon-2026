import {useEffect, useLayoutEffect, useRef, useState} from 'react';
import {useReducedMotion} from '@mantine/hooks';

const steps = [
  {label: 'Receive', text: 'A payment service times out. Its webhook sends the failure to Alert Funnel.', duration: 2600},
  {label: 'Group', text: 'The service times out twice more. A team rule groups all three failures into one alert.', duration: 3200},
  {label: 'Notify', text: 'The on-call engineer gets a text message with a link to the alert.', duration: 2500},
  {label: 'Resolve', text: 'The engineer opens the runbook, checks the payment service and closes the alert.', duration: 3000},
];
const viewedKey = 'af-product-walkthrough-viewed';
const lastStep = steps.length - 1;

function rememberWalkthrough() {
  try { window.sessionStorage.setItem(viewedKey, '1'); }
  catch { /* Playback is available when storage is disabled. */ }
}

// Project the circular cross sections of a funnel into the illustration plane.
function surface(t: number, angle: number) {
  const radius = 12 + 64 * (1 - t) ** 1.7;
  const height = Math.sin(angle) * radius;
  return {
    x: 214 + t * 165 + Math.cos(angle) * radius * .38 + height * .25,
    y: 106 - t * 12 + height,
  };
}

function drawSignal(context: CanvasRenderingContext2D, step: number) {
  const ctx = context;
  ctx.clearRect(0, 0, 530, 208);
  const halo = ctx.createRadialGradient(270, 105, 7, 270, 105, 170);
  halo.addColorStop(0, 'rgba(133, 108, 248, .28)');
  halo.addColorStop(1, 'rgba(100, 82, 185, 0)');
  ctx.fillStyle = halo;
  ctx.fillRect(0, 0, 530, 208);
  ctx.lineWidth = .85;
  for (let strand = 0; strand < 17; strand++) {
    const spread = (strand - 8) / 8;
    const origin = 104 + spread * 106;
    const end = surface(0, Math.asin(spread));
    const ink = ctx.createLinearGradient(0, 0, end.x, 0);
    ink.addColorStop(0, 'rgba(141, 159, 255, 0)');
    ink.addColorStop(.45, step === 0 ? 'rgba(160, 172, 255, .70)' : 'rgba(157, 172, 238, .43)');
    ink.addColorStop(1, 'rgba(205, 213, 255, .5)');
    ctx.strokeStyle = ink;
    ctx.beginPath();
    ctx.moveTo(0, origin);
    ctx.bezierCurveTo(80, origin - 12, 136, end.y + 8, end.x, end.y);
    ctx.stroke();
  }

  // Longitudinal lines and a stack of ellipses make the narrowing signal path readable when paused.
  for (let side = 0; side < 24; side++) {
    const angle = side * Math.PI / 12;
    ctx.strokeStyle = side % 3 === 0 ? 'rgba(205, 195, 255, .56)' : 'rgba(169, 150, 241, .25)';
    ctx.beginPath();
    for (let n = 0; n <= 32; n++) {
      const p = surface(n / 32, angle);
      if (n === 0) ctx.moveTo(p.x, p.y);
      else ctx.lineTo(p.x, p.y);
    }
    ctx.stroke();
  }
  for (let ring = 0; ring <= 36; ring++) {
    const t = ring / 36;
    ctx.strokeStyle = ring === 0 ? '#e4ddff' : `rgba(181, 163, 255, ${(step === 1 ? .82 : .61) - t * .19})`;
    ctx.lineWidth = ring === 0 ? 2 : .85;
    ctx.beginPath();
    for (let segment = 0; segment <= 72; segment++) {
      const p = surface(t, segment * Math.PI / 36);
      if (segment === 0) ctx.moveTo(p.x, p.y);
      else ctx.lineTo(p.x, p.y);
    }
    ctx.closePath();
    ctx.stroke();
  }

  const beam = ctx.createLinearGradient(376, 0, 488, 0);
  beam.addColorStop(0, '#b29cff');
  beam.addColorStop(1, '#8cebd0');
  ctx.strokeStyle = beam;
  ctx.lineWidth = step >= 2 ? 2.6 : 1.4;
  ctx.beginPath();
  ctx.moveTo(379, 94);
  ctx.lineTo(481, 94);
  ctx.stroke();
  ctx.fillStyle = '#202844';
  ctx.strokeStyle = step >= 2 ? '#9cf8d9' : '#88baae';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.roundRect(478, 80, 28, 28, 8);
  ctx.fill();
  ctx.stroke();
  ctx.beginPath();
  if (step === lastStep) {
    ctx.moveTo(485, 94); ctx.lineTo(490, 99); ctx.lineTo(499, 88);
  } else if (step === 2) {
    ctx.rect(484, 89, 16, 11);
    ctx.moveTo(484, 89); ctx.lineTo(492, 95); ctx.lineTo(500, 89);
  } else {
    ctx.moveTo(485, 94); ctx.lineTo(498, 94);
    ctx.moveTo(494, 90); ctx.lineTo(498, 94); ctx.lineTo(494, 98);
  }
  ctx.stroke();
}

function SignalDiagram({step, playing}: {step: number; playing: boolean}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useLayoutEffect(() => {
    const node = canvas.current;
    const ctx = node?.getContext('2d');
    if (!node || !ctx) return;
    function paint() {
      if (!node || !ctx) return;
      const scale = Math.min(window.devicePixelRatio || 1, 2);
      node.width = Math.max(1, Math.round(node.clientWidth * scale));
      node.height = Math.max(1, Math.round(node.clientHeight * scale));
      ctx.setTransform(node.width / 530, 0, 0, node.height / 208, 0, 0);
      drawSignal(ctx, step);
    }
    paint();
    const observer = new ResizeObserver(paint);
    observer.observe(node);
    return () => observer.disconnect();
  }, [step]);
  return <figure className="signal-figure" aria-label="Several event streams converge into a grouped alert and reach the on-call team">
    <div className="signal-drawing" data-step={step} data-playing={playing}>
      <canvas ref={canvas} aria-hidden="true"/>
      <i key={step} className="signal-bead" aria-hidden="true"/>
    </div>
    <figcaption><span data-current={step === 0}>{step === 0 ? 'Webhook' : '3 timeouts'}</span>
      <span data-current={step === 1}>1 grouped alert</span>
      <span data-current={step >= 2}>{step === lastStep ? 'Runbook' : step === 2 ? 'SMS to on-call' : 'On call'}</span></figcaption>
  </figure>;
}

export function SignalWalkthrough() {
  const reduced = useReducedMotion(undefined, {getInitialValueInEffect: false});
  const [step, setStep] = useState(0);
  const [playing, setPlaying] = useState(() => {
    if (reduced) return false;
    try { return !window.sessionStorage.getItem(viewedKey); }
    catch { return false; }
  });

  useEffect(() => { if (reduced) setPlaying(false); }, [reduced]);
  useEffect(() => { if (playing) rememberWalkthrough(); }, [playing]);
  useEffect(() => {
    if (!playing) return;
    const timer = window.setTimeout(() => {
      if (step === lastStep) {
        setPlaying(false);
      } else setStep(step + 1);
    }, steps[step].duration);
    return () => window.clearTimeout(timer);
  }, [playing, step]);

  function selectStep(index: number) {
    setPlaying(false);
    setStep(index);
    rememberWalkthrough();
  }
  function toggle() {
    if (!playing && step === lastStep) setStep(0);
    setPlaying(!playing);
  }
  const playback = playing ? 'Pause' : step === lastStep ? 'Replay' : 'Play';
  return <section className="signal-story" id="workflow" aria-labelledby="signal-heading">
    <div className="story-header">
      <div>
        <span className="story-label">Workflow example</span>
        <h2 id="signal-heading">Payment service timeout</h2>
      </div>
      <button type="button" className="story-play" aria-label={`${playback} walkthrough`} onClick={toggle}>
        <span className={playing ? 'pause-icon' : 'play-icon'} aria-hidden="true"/>{playback}
      </button>
    </div>
    <SignalDiagram step={step} playing={playing}/>
    <p className="story-description" aria-live={playing ? 'off' : 'polite'}>{steps[step].text}</p>
    <div className="story-stages" role="group" aria-label="Workflow steps">
      {steps.map((item, index) => <button key={item.label} type="button" aria-pressed={step === index}
        onClick={() => selectStep(index)}><i aria-hidden="true"/>{item.label}</button>)}
    </div>
  </section>;
}
