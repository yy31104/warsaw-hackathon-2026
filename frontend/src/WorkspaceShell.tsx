import {useEffect, useRef, useState, type ReactNode} from 'react';
import {useComputedColorScheme, useMantineColorScheme} from '@mantine/core';
import {Link, useLocation} from 'react-router-dom';
import {ALERTS_MOCK, MOCK_MODE} from './api';
import {FunnelMark, Glyph} from './Icons';
import {PageErrorBoundary} from './PageErrorBoundary';

const sections = [
  {label: 'Monitor', links: [
    ['/overview', 'Overview', 'grid'],
    ['/alerts', 'Live alerts', 'pulse'],
    ['/dashboard', 'Event funnel', 'funnel'],
    ['/approvals', 'Approvals', 'check'],
  ]},
  {label: 'Configure', links: [
    ['/rules', 'Routing rules', 'sliders'],
    ['/people', 'Preferences', 'person'],
    ['/onboarding', 'Event formats', 'braces'],
    ['/runbooks', 'Runbooks', 'book'],
    ['/proposals', 'Rule proposals', 'edit'],
  ]},
] as const;

type ShellProps = {
  live: string;
  last: string;
  risk?: boolean;
  controls: ReactNode;
  children: ReactNode;
};

export function WorkspaceShell({live, last, risk, controls, children}: ShellProps) {
  const {pathname} = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuToggle = useRef<HTMLButtonElement>(null);
  const scheme = useComputedColorScheme('light');
  const {toggleColorScheme} = useMantineColorScheme();
  const currentPath = pathname.startsWith('/a/') ? '/alerts' : pathname;
  const current = sections.flatMap(section => [...section.links]).find(([url]) => url === currentPath);
  const pageName = pathname.startsWith('/a/') ? 'Incident detail' : (current?.[1] ?? 'Workspace');
  const dataMode = MOCK_MODE === ALERTS_MOCK ? (MOCK_MODE ? 'Demo data' : 'API data') : 'Mixed data';
  const isLive = live === 'Live';

  useEffect(() => {
    setMenuOpen(false);
    document.title = `${pageName} · Alert Funnel`;
    window.scrollTo(0, 0);
  }, [pathname, pageName]);

  function closeOnEscape(event: React.KeyboardEvent) {
    if (event.key === 'Escape' && menuOpen) {
      setMenuOpen(false);
      menuToggle.current?.focus();
    }
  }

  return <div className="ops-shell" onKeyDown={closeOnEscape}>
    <a className="skip-content" href="#workspace-content">Skip to content</a>
    <div className="ops-rail">
      <div className="rail-brand-row">
        <Link to="/" className="ops-brand" aria-label="Alert Funnel home" onClick={() => setMenuOpen(false)}>
          <FunnelMark/>
          Alert Funnel
        </Link>
        <button type="button" ref={menuToggle} className="rail-menu-button" aria-expanded={menuOpen}
          aria-controls="workspace-menu" onClick={() => setMenuOpen(!menuOpen)}>
          {menuOpen ? 'Close' : 'Menu'} <Glyph name="menu"/>
        </button>
      </div>
      <div className="rail-body" id="workspace-menu" data-open={menuOpen}>
        <nav className="ops-nav" aria-label="Main navigation">
          {sections.map(section => <div className="rail-group" key={section.label}>
            <div className="rail-label">{section.label}</div>
            {section.links.map(([path, label, glyph]) => <Link key={path} to={path}
              onClick={() => setMenuOpen(false)} aria-current={currentPath === path ? 'page' : undefined}>
              <Glyph name={glyph}/>{label}
            </Link>)}
          </div>)}
        </nav>
        <div className="rail-footer">
          <button type="button" onClick={() => toggleColorScheme()} aria-label="Toggle theme">
            <Glyph name="theme"/>{scheme === 'dark' ? 'Light appearance' : 'Dark appearance'}
          </button>
        </div>
      </div>
    </div>
    <div className="ops-workspace">
      <header className="ops-toolbar">
        <div className="workspace-path"><Glyph name="workspace"/><span>Workspace</span><b>/</b>
          <strong>{pageName}</strong>
        </div>
        <div className="scope-controls">{controls}</div>
        <span className="stream-state" data-live={isLive} title={`${dataMode} · Last event: ${last}`}>
          <i aria-hidden="true"/>{live}
        </span>
      </header>
      <main id="workspace-content" tabIndex={-1} className="ops-content">
        {risk === true && <Link className="risk-notice" to="/dashboard">Delivery is at risk. Review the event funnel →</Link>}
        <PageErrorBoundary key={pathname}>{children}</PageErrorBoundary>
        <details className="ops-connection"><summary>{dataMode} <span>· Connection details</span></summary>
          <p>Alert data comes from {ALERTS_MOCK ? 'the demo fixtures' : 'the API'}. Configuration comes from {MOCK_MODE ? 'the demo workspace' : 'the API'}.
            {' '}Stream: {live}. Last event: {last}.</p>
          {risk !== undefined && <p>Delivery is {risk ? 'outside' : 'within'} its target.</p>}
        </details>
      </main>
    </div>
  </div>;
}
