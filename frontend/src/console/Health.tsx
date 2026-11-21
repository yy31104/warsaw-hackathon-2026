import {useQuery} from '@tanstack/react-query';
import {benchmark} from './benchmark';
import {useFunnel} from './data';
import {BenchmarkSource, Card, Chip, countLabel, Failure, Icon, LegacyLink} from './ui';

type SaturationRun = {
  status: string; started_at_ms: number; normal_workers: number; normal_keys: number;
  normal_offer_per_s: number; critical_offer_per_s: number; processed_steady_per_s: number;
};
type Batch = {runs?: SaturationRun[]};
const saturationReports = import.meta.glob<Batch>('../../../loadtest/results/saturation-*/batch.json', {import: 'default', eager: true});
const lineStyles = ['none', '6 3', '2 3'];

function Saturation() {
  const reports = Object.entries(saturationReports).filter(([, batch]) => batch.runs?.some(run => run.status === 'PASS'));
  reports.sort(([, a], [, b]) => Math.max(...b.runs!.map(run => run.started_at_ms))
    - Math.max(...a.runs!.map(run => run.started_at_ms)));
  if (!reports.length) return <Card title="Max load"><p>Not measured yet</p></Card>;
  const [file, batch] = reports[0];
  const rows = batch.runs!.filter(run => run.status === 'PASS' && Number.isFinite(run.processed_steady_per_s));
  if (!rows.length) return <Card title="Max load"><p>Not measured yet</p></Card>;
  const keyCounts = [...new Set(rows.map(run => run.normal_keys))].sort((a, b) => a - b);
  const fileLabel = file.split('/').slice(-2).join('/');
  const offered = (item: SaturationRun) => item.normal_offer_per_s + item.critical_offer_per_s;
  const xmax = Math.max(1, ...rows.map(offered));
  const ymax = Math.max(1, ...rows.map(run => run.processed_steady_per_s));
  return <Card title="Max load" meta={`benchmark · N=${rows.length} runs`}>
    <div className="cf-grid">
      {keyCounts.map(keys => {
        const group = rows.filter(run => run.normal_keys === keys);
        const workers = [...new Set(group.map(run => run.normal_workers))].sort((a, b) => a - b);
        return <div key={keys}><h3>{keys === 1 ? 'One hot key' : countLabel(keys) + ' keys'}</h3>
          <svg className="cf-saturation" viewBox="0 0 310 145" role="img"
            aria-label={`Measured processing throughput for ${keys} ${keys === 1 ? 'key' : 'keys'}`}>
            <path d="M35 10v115h266" className="cf-chart-axis"/>
            <text x="0" y="13">{countLabel(Math.ceil(ymax))}</text>
            <text x="35" y="138">0</text>
            <text x="302" y="138" textAnchor="end">{countLabel(xmax)}</text>
            {workers.map((worker, index) => {
              const points = group.filter(run => run.normal_workers === worker).sort((a, b) => offered(a) - offered(b));
              return <polyline strokeDasharray={lineStyles[index % lineStyles.length]} key={worker}
                points={points.map(run => [35 + 266 * offered(run) / xmax, 125 - 115 * run.processed_steady_per_s / ymax].join(',')).join(' ')}>
                <title>{worker} workers</title>
              </polyline>;
            })}
          </svg>
          <div className="cf-meta">Offered → processed events/s</div>
          <ul className="cf-chart-legend" aria-label="Workers">{workers.map((worker, index) => <li key={worker}>
            <svg viewBox="0 0 18 8" aria-hidden="true"><line x1="0" y1="4" x2="18" y2="4"
              strokeDasharray={lineStyles[index % lineStyles.length]}/></svg>{worker} {worker === 1 ? 'worker' : 'workers'}
          </li>)}</ul>
          <details><summary>Measured points</summary>
            <ul>{group.map((run, index) => <li key={index}>{run.normal_workers} workers · {countLabel(offered(run))} → {countLabel(run.processed_steady_per_s)}</li>)}</ul>
          </details>
        </div>;
      })}
    </div>
    <small>loadtest/results/{fileLabel}</small>
  </Card>;
}

async function systemRead(path: string) {
  const base = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '');
  const target = !base && path === '/health' ? '/ops-health' : base + path;
  const response = await fetch(target, {signal: AbortSignal.timeout(10000)});
  if (!response.ok) throw new Error(`Service check returned ${response.status}.`);
  return response;
}

export function Health() {
  const funnel = useFunnel();
  const state = useQuery({queryKey: ['console-health'], queryFn: async () =>
    (await systemRead('/health')).json() as Promise<{status?: string}>, refetchInterval: 10000});
  const metrics = useQuery({queryKey: ['console-metrics'], queryFn: async () =>
    (await systemRead('/metrics')).text(), refetchInterval: 10000});
  const stored = metrics.data?.match(/^events_ingested_total (\d+)$/m)?.[1];
  const processed = metrics.data?.match(/^events_processed_total (\d+)$/m)?.[1];
  const data = funnel.data;
  const stages = data && [
    {label: 'Received', value: data.raw, meta: '24 h'},
    {label: 'Waiting', value: data.processing_backlog, meta: 'inbox'},
    {label: 'Open', value: data.alerts_open, meta: 'incidents'},
    {label: 'Unsent', value: data.backlog, meta: 'outbox'},
  ];
  return <div className="cf-stack">
    <Failure error={funnel.error || state.error || metrics.error}/>
    <Card title="Pipeline" meta="live">
      {stages && <dl className="cf-pipeline">{stages.map(stage => <div key={stage.label}>
        <dd>{countLabel(stage.value)}</dd><dt>{stage.label}</dt><small>{stage.meta}</small>
      </div>)}</dl>}
      <div className="cf-chips">
        {state.data?.status === 'ok' && <Chip tone="green"><Icon name="check"/>API healthy</Chip>}
        {data && <Chip tone={data.at_risk ? 'red' : 'green'}>{data.at_risk ? 'Delivery queue delayed' : 'Delivery queue on time'}</Chip>}
        {data && <Chip>Oldest pending · {Math.round(data.oldest_pending_s)} s</Chip>}
        {stored && processed && <Chip>{countLabel(Number(processed))}/{countLabel(Number(stored))} processed · total</Chip>}
      </div>
    </Card>
    <div className="cf-grid cf-grid-wide">
      <Card title="Event to phone" meta={<BenchmarkSource file="README.md">{benchmark.label} · N={benchmark.one.n}</BenchmarkSource>}>
        <div className="cf-bench-bars">{[
          ['p50', benchmark.one.p50_ms], ['p95', benchmark.one.p95_ms],
          ['p99', benchmark.one.p99_ms], ['2 workers p99', benchmark.two.p99_ms],
        ].map(([label, latency]) => <div key={label}>
          <span>{label}</span><meter min={0} max={benchmark.one.p99_ms} value={Number(latency)}
            aria-label={String(label)}/><strong>{latency} ms</strong>
        </div>)}</div>
        <small>{benchmark.dropped} dropped · {countLabel(benchmark.inputs)} events · {benchmark.offeredPerSecond}/s offered</small>
      </Card>
      <Card title={`Gateway down ${benchmark.outage.seconds} s`}
        meta={<BenchmarkSource file="telecom-outage/summary.json">{benchmark.label} · N={benchmark.outage.expected}</BenchmarkSource>}>
        <div className="cf-big cf-success">{benchmark.outage.lost} <small>lost</small></div>
        <p>{benchmark.outage.delivered}/{benchmark.outage.expected} delivered after recovery</p>
        <small>SMS and phone</small>
      </Card>
    </div>
    <Saturation/>
    <Card title="What's simulated"><div className="cf-chips">
      {['Fake telecom', 'Mailpit', 'Mock ops', 'Demo sign-in'].map(item => <Chip key={item}>{item}</Chip>)}
    </div></Card>
    <LegacyLink to="/dashboard">Delivery details</LegacyLink>
  </div>;
}
