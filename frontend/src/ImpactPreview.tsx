export type ImpactPreviewResult = {
  events_replayed: number;
  current: {alerts: number; notifications: Record<string, number>};
  draft: {alerts: number; notifications: Record<string, number>};
  lost_all_notifications: number[];
  lost_recipients?: string[];
  replay_basis?: string;
};

export function ImpactPreview({result}: {result: ImpactPreviewResult}) {
  const keys = [...new Set([...Object.keys(result.current.notifications), ...Object.keys(result.draft.notifications)])].sort();
  const missed = result.lost_all_notifications;
  return <section aria-label="Rule impact">
    <h3>Impact preview</h3>
    <p>{result.events_replayed} historical events replayed. Alerts {result.current.alerts} → {result.draft.alerts}.</p>
    {result.replay_basis && <p>{result.replay_basis}</p>}
    {keys.length > 0 && <table><thead><tr><th>Recipient and channel</th><th>Current</th><th>Draft</th></tr></thead><tbody>
      {keys.map(key => <tr key={key}><td>{key}</td><td>{result.current.notifications[key] ?? 0}</td><td>{result.draft.notifications[key] ?? 0}</td></tr>)}
    </tbody></table>}
    <p className={missed.length ? 'validation-danger' : ''}>{missed.length} replayed events would lose all notifications.</p>
    {!!result.lost_recipients?.length && <p className="validation-danger">Recipients removed or sent fewer notifications: {result.lost_recipients.join(', ')}.</p>}
    {missed.length > 0 && <details><summary>Affected event IDs</summary><p>{missed.join(', ')}</p></details>}
  </section>;
}
