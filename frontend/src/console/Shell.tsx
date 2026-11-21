import {Component, useCallback, useState, type ReactNode} from 'react';
import {Link, NavLink, useLocation} from 'react-router-dom';
import {useMantineColorScheme} from '@mantine/core';
import {useQuery} from '@tanstack/react-query';
import {useApp} from '../App';
import {request} from '../api';
import type {Directory} from '../types';
import {canReview, useConsoleQueue} from './data';
import {Intro, readSession, writeSession} from './Intro';
import {Tour} from './Tour';
import {Card, Icon, personName, teamName, timeLabel, useClock} from './ui';
import './console.css';

const pages = [
  {path: '/', title: 'Now', icon: 'now', group: 'Respond', note: 'Incidents that need attention'},
  {path: '/incidents', title: 'Incidents', icon: 'incidents', group: 'Respond', note: 'Open and past incidents'},
  {path: '/sources', title: 'Sources', icon: 'sources', group: 'Set up', note: 'Event formats waiting for review'},
  {path: '/rules', title: 'Rules', icon: 'rules', group: 'Set up', note: 'When alerts fire and who is notified'},
  {path: '/runbooks', title: 'Runbooks', icon: 'runbooks', group: 'Set up', note: 'Response steps for each alert'},
  {path: '/oncall', title: 'On call', icon: 'oncall', group: 'Set up', note: 'Who is on call and how to reach them'},
  {path: '/health', title: 'Health', icon: 'health', group: 'Check', note: 'Pipeline health and measured performance'},
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
  const [tour, setTour] = useState<number | null>(null);
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
  const page = pages.find(item => item.path === pathname || (item.path !== '/' && pathname.startsWith(item.path + '/')));
  const title = legacyTitles[pathname] ?? page?.title ?? 'Console';
  const note = legacyTitles[pathname] ? undefined : page?.note;

  return <div className="console">
    <a className="cf-skip" href="#console-content">Skip to content</a>
    <nav className="cf-rail" aria-label="Main navigation">
      <Link to="/" className="cf-brand" aria-label="Alert Funnel"><span className="cf-logo"><Icon name="funnel"/></span>
        <span className="cf-wordmark">Alert Funnel</span></Link>
      {pages.map((item, index) => <div className="cf-rail-item" key={item.path}>
        {item.group !== pages[index - 1]?.group && <span className="cf-rail-group" aria-hidden="true">{item.group}</span>}
        <NavLink to={item.path} end={item.path === '/'}>
          <Icon name={item.icon}/><span>{item.title}</span>
          {item.path === '/' && badge > 0 && <span className="cf-badge" aria-label={`${badge} need you`}>{badge}</span>}
        </NavLink>
      </div>)}
      <div className="cf-rail-foot">
        <button className="cf-rail-intro" onClick={() => setReplay(true)}><Icon name="intro"/><span>Intro</span></button>
        <button className="cf-rail-tour" onClick={() => setTour(0)}><Icon name="tour"/><span>Tour</span></button>
      </div>
    </nav>
    <div className="cf-workspace">
      <header className="cf-top">
        <h1>{incident ? <Link to="/"><Icon name="arrow"/> INC-{incident[1]}</Link> : title}
          {!incident && note && <small className="cf-top-note">{note}</small>}</h1>
        <div className="cf-top-controls">
          <WarsawClock live={live}/>
          {assistant.data && <span className="cf-assist">{assistant.data.enabled
            ? ['AI on', assistant.data.model].filter(Boolean).join(' · ') : 'AI off · rules only'}</span>}
          {teams.length > 1 && <select aria-label="Team" value={team} onChange={event => setTeam(event.target.value)}>
            {teams.map(item => <option value={item.id} key={item.id}>{teamName(item.id, directory)}</option>)}
          </select>}
          {teams.length === 1 && <span className="cf-assist" title="The team whose incidents you see">
            {teamName(teams[0].id, directory)}</span>}
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
    {(replay || (pathname === '/' && !watched && tour === null)) && <Intro close={close} tour={() => setTour(0)}/>}
    {tour !== null && <Tour step={tour} setStep={setTour}/>}
  </div>;
}
