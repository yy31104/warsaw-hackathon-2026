import {useEffect} from 'react';
import {Link, useLocation} from 'react-router-dom';
import {FunnelMark, Glyph} from './Icons';
import {SignalWalkthrough} from './SignalWalkthrough';
import './product.css';

const tasks = [
  {
    title: 'Connect a new event format',
    description: 'Map fields from webhooks, message queues, and logs.',
    label: 'Event formats',
    path: '/onboarding',
    icon: 'braces',
  },
  {
    title: 'Choose who gets alerted',
    description: 'Decide when to send an alert and who should receive it.',
    label: 'Routing rules',
    path: '/rules',
    icon: 'sliders',
  },
  {
    title: 'Investigate an alert',
    description: 'See what happened, check delivery, and follow the runbook.',
    label: 'Live alerts',
    path: '/alerts',
    icon: 'pulse',
  },
];

export function ProductHome() {
  const {hash} = useLocation();

  useEffect(() => {
    document.title = 'Alert Funnel · Alerting and response';
    const target = hash ? document.getElementById(hash.slice(1)) : null;
    if (target) target.scrollIntoView();
    else window.scrollTo(0, 0);
  }, [hash]);

  return <div className="product-home">
    <a className="skip-content" href="#product-content">Skip to content</a>
    <header className="product-header">
      <div className="product-frame">
        <Link className="product-brand" to="/" aria-label="Alert Funnel home">
          <span className="product-brand-icon"><FunnelMark/></span>
          Alert Funnel
        </Link>
        <nav className="product-nav" aria-label="Product navigation">
          <a href="#workflow">How it works</a>
          <a href="#features">Features</a>
        </nav>
      </div>
    </header>
    <main className="product-main product-frame" id="product-content" tabIndex={-1}>
      <section className="product-hero" aria-labelledby="product-heading">
        <div className="product-intro">
          <h1 id="product-heading">Turn repeated failures into <em>one alert.</em></h1>
          <p>Alert Funnel groups related failures, notifies the on-call engineer, and shows what to do next.</p>
          <Link className="product-cta" to="/overview">Open workspace <span aria-hidden="true">→</span></Link>
        </div>
        <SignalWalkthrough/>
      </section>
      <section className="product-features" id="features" aria-labelledby="features-heading">
        <h2 id="features-heading">From setup to response</h2>
        <div className="product-tasks">
          {tasks.map(task => <Link className="product-task" key={task.path} to={task.path}>
            <span className="task-icon" aria-hidden="true"><Glyph name={task.icon}/></span>
            <h3>{task.title}</h3>
            <p>{task.description}</p>
            <span className="task-link">{task.label} <span aria-hidden="true">→</span></span>
          </Link>)}
        </div>
      </section>
    </main>
  </div>;
}
