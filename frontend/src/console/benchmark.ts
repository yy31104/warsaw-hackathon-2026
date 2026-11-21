import oneWorker from '../../../loadtest/results/platform-bench/one-normal/summary.json';
import twoWorkers from '../../../loadtest/results/platform-bench/two-normal/summary.json';
import outage from '../../../loadtest/results/platform-bench/telecom-outage/summary.json';
import downtime from '../../../loadtest/results/platform-bench/telecom-outage/outage-container.json';

export const benchmark = {
  label: 'benchmark · 20 Nov',
  one: oneWorker.receiver.e2e,
  two: twoWorkers.receiver.e2e,
  inputs: oneWorker.scheduled,
  dropped: oneWorker.dropped,
  offeredPerSecond: oneWorker.offered_normal_events_per_s + oneWorker.offered_critical_events_per_s,
  outage: {
    seconds: Math.round(downtime.stopped_before_start_seconds),
    lost: outage.receiver.lost_count,
    delivered: outage.receiver.receiver_ids.length,
    expected: outage.receiver.expected_outbox_ids.length,
  },
};
