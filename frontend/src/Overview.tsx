import {useState} from 'react';
import {Link} from 'react-router-dom';
import {Badge, Button} from '@mantine/core';
import {useQuery} from '@tanstack/react-query';
import {ErrorBox, PageTitle, Panel, useApp} from './App';
import {ALERTS_MOCK, MOCK_MODE, alertListQuery, api} from './api';
import {DemoControls, LiveTrend} from './Motion';
import {mockAction, useSession} from './session';
import {StatusOverview} from './StatusOverview';
import {ActivityFeed} from './ActivityFeed';
import {ActiveAlerts} from './ActiveAlerts';
import {Glyph} from './Icons';
import type {Alert} from './model';

function TeamResponse() {
  const {team, directory} = useApp();
  const demo = useSession();
  const demoAlerts = new Set(demo.alerts.filter(a => !team || a.team_id === team).map(a => a.id));
  const pending = ALERTS_MOCK ? demo.actions.filter(a => a.state === 'pending_approval' && demoAlerts.has(a.alert_id)).length : undefined;
  const atRisk = ALERTS_MOCK ? demo.notifications.filter(n => demoAlerts.has(n.alert_id) && ['retrying','failed','unknown'].includes(n.state)).length : undefined;
  const oncall = directory.teams.find(item => item.id === team)?.oncall?.primary;
  const person = directory.users.find(user => user.id === oncall);
  const name = person?.name.split(' (')[0] || oncall;
  return <section className="response-box" aria-labelledby="response-heading">
    <div className="box-heading"><h2 id="response-heading">Team response</h2></div>
    <div className="oncall-person"><span className="oncall-avatar" aria-hidden="true">{name ? name.charAt(0).toUpperCase() : '–'}</span>
      <div><span>Primary on call</span><strong>{name ?? (team ? 'Unassigned' : 'Select a team')}</strong></div>
    </div>
    <Link to="/people"><Glyph name="sliders"/>Notification preferences <b aria-hidden="true">→</b></Link>
    <Link to="/runbooks"><Glyph name="book"/>Team runbooks <b aria-hidden="true">→</b></Link>
    <Link to="/approvals"><Glyph name="check"/>Action approvals {pending !== undefined && <small>{pending} pending</small>}<b aria-hidden="true">→</b></Link>
    {atRisk !== undefined && <Link to="/dashboard"><Glyph name="pulse"/>Delivery health <small>{atRisk} at risk</small><b aria-hidden="true">→</b></Link>}
  </section>;
}

function GroupingDetail({alert}: {alert: Alert}) {
  const [open, setOpen] = useState(false);
  const deliveries = useQuery({queryKey: ['detail','notifications',alert.id], queryFn: () => api.notifications(alert.id), enabled: open});
  const external = deliveries.data?.filter(row => row.channel !== 'ui').length;
  return <details className="grouping-detail" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>How this team’s busiest alert was grouped</summary>
    <div><Link to={`/a/${alert.id}`}>{alert.title} →</Link>
      <p>{alert.occurrence_count.toLocaleString()} distinct occurrences are grouped under this alert. Retransmissions are counted separately.</p>
      <ErrorBox error={deliveries.error}/>{external !== undefined && <p>{external} external notification {external === 1 ? 'record' : 'records'}. Open the alert to check delivery status.</p>}
    </div>
  </details>;
}

export function Overview() {
  const {team, feedEnabled} = useApp();
  const data = useQuery(alertListQuery(team, feedEnabled));
  const active = feedEnabled ? data.data?.filter(a => (!team || a.team_id === team) && a.status !== 'resolved') : undefined;
  const busiest = active?.reduce<Alert|undefined>((largest,a) => !largest || a.occurrence_count > largest.occurrence_count ? a : largest, undefined);
  const loading = feedEnabled && data.isPending;
  const message = !feedEnabled ? 'Choose a user with an assigned team to see alerts.' : loading ? 'Loading team alerts…'
    : data.error && !data.data ? 'The alert queue is unavailable. Check the connection and try again.' : undefined;
  return <>
    <PageTitle title="Operations overview" description={team ? `Alerts and response activity for the ${team} team.` : 'Alerts and response activity across your teams.'}
      action={<Link className="primary-link" to="/alerts">Open live alerts <span aria-hidden="true">→</span></Link>}/>
    <ErrorBox error={data.error}/>
    <StatusOverview alerts={active}/>
    <div className="operations-grid"><ActiveAlerts items={active ?? []} message={message}/><TeamResponse/></div>
    {busiest && <GroupingDetail alert={busiest}/>}
    {MOCK_MODE && ALERTS_MOCK && <details className="replay-tools"><summary>Simulated activity and replay</summary>
      <div><DemoControls/><LiveTrend/><ActivityFeed/></div></details>}
  </>;
}

export function Approvals() {
  const {actor, admin, notice, team, feedEnabled} = useApp();
  const state = useSession();
  const data = useQuery(alertListQuery(team, feedEnabled && !ALERTS_MOCK));
  const reviewable = data.data?.filter(alert => alert.status !== 'resolved') ?? [];
  const requests = state.actions.filter(action => action.approvers.length > 0 &&
    (!team || state.alerts.some(alert => alert.id === action.alert_id && alert.team_id === team)));
  return <>
    <PageTitle title="Action approvals" description="Review runbook steps that need a person’s approval before they can run."
      action={<Link className="secondary-link" to="/proposals">Rule proposals →</Link>}/>
    {ALERTS_MOCK ? <Panel title="Runbook execution requests">
      {requests.map(action => {
        const allowed = action.state === 'pending_approval' && (admin || action.approvers.includes(actor));
        function decide(decision: 'approve'|'reject') {
          mockAction(action.alert_id, action.step_index, decision, actor, admin);
          notice(decision === 'approve' ? 'Approved and executed in demo mode' : 'Demo request rejected');
        }
        return <div className="approval-request" key={action.id}>
          <Badge color={action.state === 'pending_approval' ? 'orange' : 'teal'}>{action.state.replaceAll('_',' ')}</Badge>
          <h3>{action.text}</h3><p>Requested by {action.requester} · Approvers: {action.approvers.join(', ')}</p>
          <Link to={`/a/${action.alert_id}`}>Open alert #{action.alert_id} →</Link>
          <div className="actions"><Button disabled={!allowed} onClick={() => decide('approve')}>Approve</Button>
            <Button variant="default" disabled={!allowed} onClick={() => decide('reject')}>Reject</Button></div>
          {action.approver && <p>Decision by {action.approver}</p>}
        </div>;
      })}
      {!requests.length && <p>No requests yet. Request approval from an alert’s runbook.</p>}
    </Panel> : <section className="approval-guide">
      <div className="approval-intro"><span className="approval-symbol"><Glyph name="check"/></span><div>
        <h2>Review approvals inside an alert</h2><p>Open the alert, then its runbook. Each step shows who can approve it.</p>
      </div></div>
      <ErrorBox error={data.error}/>
      {feedEnabled && data.isPending && <p>Loading team alerts…</p>}
      {reviewable.length > 0 && <div className="approval-alerts"><h3>Active team alerts</h3>
        {reviewable.slice(0,5).map(alert => <Link key={alert.id} to={`/a/${alert.id}`}>{alert.title} <span>→</span></Link>)}</div>}
      <Link className="primary-link" to="/alerts">Open live alerts <span aria-hidden="true">→</span></Link>
    </section>}
  </>;
}
