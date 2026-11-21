import { check } from 'k6';
import exec from 'k6/execution';
import http from 'k6/http';
import { Counter } from 'k6/metrics';

function positiveInt(name, fallback) {
    const value = Number(__ENV[name] ?? fallback);
    if (!Number.isSafeInteger(value) || value < 1) {
        throw new Error(`${name} must be a positive integer`);
    }
    return value;
}

const rate = positiveInt('HTTP_RATE', 20);
const duration = positiveInt('HTTP_DURATION_S', 5);
const preAllocatedVUs = positiveInt('HTTP_PREALLOCATED_VUS', 5);
const maxVUs = positiveInt('HTTP_MAX_VUS', 20);
const runId = __ENV.HTTP_RUN_ID;
if (!runId || !/^[a-z0-9][a-z0-9-]{0,40}$/.test(runId)) {
    throw new Error('HTTP_RUN_ID must be a unique lowercase test tag, up to 41 characters');
}
if (maxVUs < preAllocatedVUs) {
    throw new Error('HTTP_MAX_VUS must be at least HTTP_PREALLOCATED_VUS');
}

const api = (__ENV.ALERTING_API_URL ?? 'http://api:8000').replace(/\/$/, '');
const source = __ENV.HTTP_SOURCE ?? 'generic-webhook';
const apiKey = __ENV.HTTP_API_KEY ?? 'demo-generic-key';
const template = JSON.parse(open('../fixtures/events/workflow_failed.json'));
const accepted = new Counter('http_events_accepted');
const executor = 'constant-arrival-rate';

export const options = {
    scenarios: {
        ingest: {
            executor,
            timeUnit: '1s',
            rate,
            duration: `${duration}s`,
            preAllocatedVUs,
            maxVUs,
        },
    },
    thresholds: {
        checks: ['rate==1'],
        dropped_iterations: ['count==0'],
    },
    summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'p(99)'],
};

export default function () {
    const eventId = `${runId}-${exec.scenario.iterationInTest}`;
    const response = http.post(
        `${api}/v1/events/${encodeURIComponent(source)}`,
        JSON.stringify({ ...template, workflowId: eventId, step: runId, environment: 'test' }),
        {
            headers: {
                'Content-Type': 'application/json',
                'X-Api-Key': apiKey,
                'Idempotency-Key': eventId,
                'X-Gen-Sent-At': String(Date.now()),
            },
            timeout: '5s',
            redirects: 0,
        },
    );
    let receipt;
    try {
        receipt = response.status === 202 ? response.json() : null;
    } catch {
        receipt = null;
    }
    const stored = receipt?.accepted === 1 && receipt.duplicates === 0 && receipt.conflicts === 0
        && receipt.rejected?.length === 0 && receipt.event_ids?.length === 1;
    check(stored, { '202 with one committed event': result => result });
    accepted.add(stored ? 1 : 0);
}

export function handleSummary(data) {
    const plan = {
        executor,
        source_id: source,
        run_id: runId,
        rate,
        duration_s: duration,
        preallocated_vus: preAllocatedVUs,
        maximum_vus: maxVUs,
        nominal_iterations: rate * duration,
    };
    const count = key => data.metrics[key]?.values?.count ?? 0;
    const result = {
        completed_iterations: count('iterations'),
        dropped_iterations: count('dropped_iterations'),
        http_requests: count('http_reqs'),
        accepted_events: count('http_events_accepted'),
    };
    return {
        stdout: `${JSON.stringify({ ...plan, ...result }, null, 2)}\n`,
        [__ENV.HTTP_SUMMARY_FILE ?? 'http-k6-summary.json']: JSON.stringify({ plan, result, k6: data }, null, 2) + '\n',
    };
}
