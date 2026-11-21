import {Component, useCallback, useState, type ReactNode} from 'react';
import {Link, NavLink, useLocation} from 'react-router-dom';
import {useMantineColorScheme} from '@mantine/core';
import {useQuery} from '@tanstack/react-query';
import {useApp} from '../App';
import {request} from '../api';
import type {Directory} from '../types';
import {canReview, useConsoleQueue} from './data';
import {Intro, readSession, writeSession} from './Intro';
import {Card, Icon, personName, teamName, timeLabel, useClock} from './ui';
import './console.css';

const pages = [
  {path: '/', title: 'Now', icon: 'now'},
  {path: '/sources', title: 'Sources', icon: 'sources'},
  {path: '/rules', title: 'Rules', icon: 'rules'},
  {path: '/runbooks', title: 'Runbooks', icon: 'runbooks'},
  {path: '/oncall', title: 'On call', icon: 'oncall'},
  {path: '/health', title: 'Health', icon: 'health'},
] as const;
const legacyTitles: Record<string, string> = {
  '/alerts': 'Incidents', '/approvals': 'Approvals', '/overview': 'Overview', '/onboarding': 'Mapping',
  '/people': 'Preferences', '/dashboard': 'Delivery', '/proposals': 'Proposals', '/rules/proposals': 'Proposals',
};
const introKey = 'af-console-intro';

class PageErrorBoundary extends Component<{children: ReactNode}, {failed: boolean}> {
  state = {failed: false};

  static getDerivedStateFromError() {
    return {failed: true};
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return <Card title="Page unavailable"><button className="cf-btn" onClick={() => this.setState({failed: false})}>Try again</button></Card>;
  }
}

function WarsawClock({live}: {live: string}) {
  const now = useClock(10000);
  return <span className="cf-clock" title={live}><i data-live={live === 'Live'}/>
    <time dateTime={new Date(now).toISOString()}>{timeLabel(now)}</time> Warsaw</span>;
}

export function ConsoleShell({children, live, teams, setActor, setTeam}: {
  children: ReactNode; live: string; teams: Directory['teams'];
  setActor: (actor: string) => void; setTeam: (team: string) => void;
}) {
  const {actor, team, directory, feedEnabled} = useApp();
  const {pathname} = useLocation();
  const [watched, setWatched] = useState(() => Boolean(readSession(introKey)));
  const [replay, setReplay] = useState(false);
  const close = useCallback(() => {
    writeSession(introKey, 'seen');
    setWatched(true);
    setReplay(false);
  }, []);
  const {colorScheme, toggleColorScheme} = useMantineColorScheme();
  const assistant = useQuery({queryKey: ['assist-status'], queryFn: () =>
    request<{enabled: boolean; model?: string | null}>('/assist/status')});
  const queue = useConsoleQueue(team, feedEnabled);
  const badge = (queue.open.data?.length ?? 0) + queue.actions.filter(({action}) => canReview(action, actor)).length;
  const incident = /^\/a\/(\d+)(?:\/details)?$/.exec(pathname);
  const title = legacyTitles[pathname] ?? pages.find(page => page.path === pathname
    || (page.path !== '/' && pathname.startsWith(page.path + '/')))?.title ?? 'Console';

  return <div className="console">
    <a className="cf-skip" href="#console-content">Skip to content</a>
    <nav className="cf-rail" aria-label="Main navigation">
      <Link to="/" className="cf-logo" aria-label="Alert Funnel"><Icon name="funnel"/></Link>
      {pages.map(page => <NavLink key={page.path} to={page.path} end={page.path === '/'}>
        <Icon name={page.icon}/>{page.title}
        {page.path === '/' && badge > 0 && <span className="cf-badge" aria-label={`${badge} need you`}>{badge}</span>}
      </NavLink>)}
      <button className="cf-rail-intro" onClick={() => setReplay(true)}><Icon name="intro"/>Intro</button>
    </nav>
    <div className="cf-workspace">
      <header className="cf-top">
        <h1>{incident ? <Link to="/"><Icon name="arrow"/> INC-{incident[1]}</Link> : title}</h1>
        <div className="cf-top-controls">
          <WarsawClock live={live}/>
          {assistant.data && <span className="cf-assist">{assistant.data.enabled
            ? ['AI on', assistant.data.model].filter(Boolean).join(' · ') : 'AI off · rules only'}</span>}
          {teams.length > 1 && <select aria-label="Team" value={team} onChange={event => setTeam(event.target.value)}>
            {teams.map(item => <option value={item.id} key={item.id}>{teamName(item.id, directory)}</option>)}
          </select>}
          {directory.users.length > 0 && <label className="cf-view-as"><span>Viewing as</span>
            <select aria-label="Viewing as" value={actor} onChange={event => setActor(event.target.value)}>
              {directory.users.map(person => <option key={person.id} value={person.id}>{personName(person.id, directory)}</option>)}
            </select>
          </label>}
          <button className="cf-btn cf-theme" type="button" aria-label="Toggle theme" onClick={() => toggleColorScheme()}>
            {colorScheme === 'dark' ? '☼' : '◐'}
          </button>
        </div>
      </header>
      <main id="console-content" className="cf-content" tabIndex={-1}>
        <PageErrorBoundary key={pathname}>{children}</PageErrorBoundary>
      </main>
    </div>
    {(replay || (pathname === '/' && !watched)) && <Intro close={close}/>}
  </div>;
}
