# Scaling the alert pipeline

The first queue to grow depends on the shape of the incident. Independent alert keys put pressure on rule processing. Repeated updates to one alert expose intake capacity first. Sending many distinct alerts to one recipient can leave notifications waiting after rules have caught up. [Measured curves](../loadtest/results/saturation-ae69a28/REPORT.md) · [Per-run counters](../loadtest/results/saturation-ae69a28/runs.csv)

## Rule processing and PostgreSQL

These are sustained **processed events/s after warm-up**, including the critical lane, for 1,000 normal alert keys. Each cell used one intake consumer and one delivery worker. The generator also offered two critical events/s during each 60-second trial. The archived CSV separately reports the whole-window rate, CPU, both lanes’ receiver percentiles, drops and pending work. [Workload and machine](../loadtest/results/saturation-ae69a28/machine.json) · [Rate definitions and measurements](../loadtest/results/saturation-ae69a28/runs.csv)

| Normal offer/s | 1 rule worker | 2 rule workers | 4 rule workers | Evidence |
| ---: | ---: | ---: | ---: | --- |
| 700 | 646.0 | 700.4 | 701.7 | [Run summaries](../loadtest/results/saturation-ae69a28/batch.json) |
| 1,400 | 626.0 | 1,170.0 | 1,405.9 | [Run summaries](../loadtest/results/saturation-ae69a28/batch.json) |
| 2,800 | 594.0 | 1,038.0 | 1,714.0 | [Run summaries](../loadtest/results/saturation-ae69a28/batch.json) |
| 5,600 | 577.3 | 1,077.2 | 1,789.1 | [Run summaries](../loadtest/results/saturation-ae69a28/batch.json) |

The **backlog knee** moves roughly with the worker count. Its first rising-backlog load doubles from 700 to 1,400 to 2,800 normal events/s as the worker count doubles from one to two to four. Achieved throughput scales sublinearly across those workers at a fixed high offer. At 2,800/s PostgreSQL mean CPU rises from 96.7% to 149.4% to 221.7% (100% denotes one logical core). The rule inbox accumulates work with four workers while the broker queue stays small. The repeat at 1,400/s independently reproduces the worker effect. PostgreSQL CPU is a shared scaling constraint. Its hard ceiling and the fraction of time attributable to any one SQL statement were **not measured**. [Knee definition and backlog medians](../loadtest/results/saturation-ae69a28/REPORT.md) · [CPU by trial](../loadtest/results/saturation-ae69a28/runs.csv) · [Worker repeat](../loadtest/results/saturation-ae69a28/diagnostic-rules/batch.json)

## One hot key and competing consumers

At 5,600 normal events/s with one hot key and four rule workers, adding a second AMQP consumer raised measured intake from 3,571.0 to 5,546.8 events/s and reduced the late broker-ready median from 99,608.5 messages to 157.5. The rule inbox then grew to a late median of 88,160.5 pending events. These matched-input trials locate the first limit in broker-to-inbox intake and the next one in rule processing. Advisory-lock waits were observed. A single alert key serializes its updates even when other workers can claim batches. [One consumer](../loadtest/results/saturation-ae69a28/diagnostic-intake1/m11-11210200-9f20c-r5600-k1-w4/summary.json) · [Two consumers](../loadtest/results/saturation-ae69a28/diagnostic-intake2/m11-11210202-ea57d-r5600-k1-w4/summary.json) · [PostgreSQL wait samples](../loadtest/results/saturation-ae69a28/diagnostic-intake2/m11-11210202-ea57d-r5600-k1-w4/samples.json)

**One hot key, 5,600 normal events/s.** Changing from one to two intake consumers reduced observed critical receiver p99 from **28.449 s** (206 of 240 receipts) to **1.233 s** (238 of 240). Outstanding receipts at cutoff have no measured latency. [One consumer](../loadtest/results/saturation-ae69a28/diagnostic-intake1/m11-11210200-9f20c-r5600-k1-w4/summary.json) · [Two consumers](../loadtest/results/saturation-ae69a28/diagnostic-intake2/m11-11210202-ea57d-r5600-k1-w4/summary.json)

**1,000 keys, 5,600 normal events/s.** Critical receiver p99 with **two intake consumers: not measured**. The matrix used one intake consumer: critical p99 was 27.437, 28.604 and 34.156 s with one, two and four normal rule workers, respectively. [Matrix configurations and receipts](../loadtest/results/saturation-ae69a28/runs.csv)

**1,000 keys, 2,800 normal events/s.** With one intake consumer and four normal rule workers, the rule inbox grew to a late-window median of **56,040.5 pending events** while critical receiver p99 stayed at **0.443 s** (240 receipts). In this run the critical lane was isolated from the normal rule backlog. [Backlog and receiver measurements](../loadtest/results/saturation-ae69a28/m11-11210120-1d623-r2800-k1000-w4/summary.json)

## Delivery and loss accounting

With 1,000 distinct normal alerts routed to one recipient, normal receiver p99 spanned **36.394–61.134 s**. In the controlled 700/s repeat, increasing delivery workers from one to four reduced p99 from **39.877 to 33.627 s** and the middle-block delivery backlog from **407.5 to 53**, while the rule inbox stayed steady. Delivery and recipient fan-out deserve separate capacity measurements from event processing. [Normal receiver series](../loadtest/results/saturation-ae69a28/runs.csv) · [One delivery worker](../loadtest/results/saturation-ae69a28/diagnostic-delivery1/batch.json) · [Four delivery workers](../loadtest/results/saturation-ae69a28/diagnostic-delivery4/batch.json)

Of **3,782,880** scheduled events, RabbitMQ confirmed **3,775,341**. The difference of **7,539** was dropped at the generator before broker acceptance. The publisher recorded **zero errors** and ingestion recorded **zero rejections**. The bounded observation can leave broker-confirmed events and expected receiver receipts pending. A claim of permanent downstream loss or lossless eventual delivery would require following those IDs to terminal state. [Aggregate counts](../loadtest/results/saturation-ae69a28/REPORT.md) · [Per-run reconciliation and pending counts](../loadtest/results/saturation-ae69a28/runs.csv)

## Apple M4 Pro cross-machine check

On the second host, one normal rule worker processed a 60-second, 1,000-key offer. Each row reports throughput across the **entire offer window** and latency to the mock SMS/phone receiver. CPU is mean PostgreSQL usage as a percentage of one core. All accepted events and expected receipts reconciled after drain in both trials. The Mac used a later source commit, omitted critical traffic, and had desktop apps running. These runs independently check which queue grows. They cannot establish a hardware speed ratio or critical latency. [Mac configuration](../loadtest/results/saturation-10c398e-m4-pro/machine.json) · [Generator command](../loadtest/results/saturation-10c398e-m4-pro/k1000-w1-r700/run.json)

| Offered/s | Processed/s | Receiver p50 / p95 / p99 (s) | Inbox backlog at 15 / 30 / 45 / 60 s | Generator drops | PG CPU | Evidence |
| ---: | ---: | --- | --- | ---: | ---: | --- |
| 700 | 698.25 | 16.034 / 24.275 / 24.748 | 140 / 210 / 175 / 105 | 0 | 77.18% | [Counters and receipt reconciliation](../loadtest/results/saturation-10c398e-m4-pro/k1000-w1-r700/metrics.json) |
| 1,400 | 1,007.83 | 15.165 / 24.906 / 25.704 | 6,130 / 12,530 / 18,330 / 23,530 | 0 | 96.68% | [Counters and receipt reconciliation](../loadtest/results/saturation-10c398e-m4-pro/k1000-w1-r1400/metrics.json) |

## What another replica can do

| Component | Work another replica can take | Shared state and limits |
| --- | --- | --- |
| API | Serve HTTP requests behind a load balancer. Give each process its own connection pool. | Rules, preferences, event identities and alert decisions live in PostgreSQL. Each SSE client stays on its serving process. Check pool use and reconnect/fan-out cost when adding instances. The demo Compose port mapping needs a load balancer or distinct host ports. [HTTP API](../backend/alerting/api/app.py) · [Event stream](../backend/alerting/api/routes_stream.py) |
| Ingest | HTTP replicas accept independent requests. RabbitMQ consumers compete for deliveries and acknowledge after the durable inbox commit. | Replays meet the same database identity constraint. Log readers can process independent files concurrently, while readers of the same source/file share a locked cursor and advance it in the inbox transaction. [Broker](../backend/alerting/ingest/amqp.py) · [File checkpoint](../backend/alerting/ingest/logtail.py) |
| Rule workers | Workers in the same lane claim different inbox rows with `FOR UPDATE SKIP LOCKED`. | Claims last for the processing transaction. A rollback unlocks the batch. Distinct alert keys can progress independently. Updates for one `(rule_id, alert_key)` take the same advisory lock and lock the active alert row. Batch aggregation reduces writes for repeated keys. [Rule worker](../backend/alerting/rules/worker.py) |
| Delivery workers | Workers claim eligible outbox rows by channel and team, commit the lease, then call the provider. | The lease records its owner and expiry. A replacement can reclaim expired work. Only the current owner can commit the result. Provider retries can repeat a request after an ambiguous timeout, so receiver idempotency still matters. [Fair claim](../backend/alerting/delivery/fair.py) · [Delivery worker](../backend/alerting/delivery/worker.py) |

The demo has one PostgreSQL primary. Losing it stops durable intake, decisions and delivery progress. More application workers add connections, WAL and competition for CPU and I/O on this same primary. A second normal worker also cannot advance the same open alert while the first holds its group lock. The default RabbitMQ node and the simulated receivers are additional dependencies. [Compose topology](../docker-compose.yml)

## Capacity planning

These are design scenarios. Throughput, latency and cost at each multiplier are **not measured**. Size against the sustained rate that meets the receiver latency objective and has stable backlog for the relevant key distribution.

| Target | First change to evaluate | Evidence required before committing capacity |
| --- | --- | --- |
| 10× · not measured | Add rule workers while the inbox grows and PostgreSQL CPU has headroom. Scale intake or delivery when its queue grows. Bound total database connections and provider concurrency. If repeated keys dominate, improve per-batch aggregation and tune work per claim. | Re-run the same generator and payload mix. Track oldest pending age, rejected input, worker wait events and receiver latency. Compare independent keys with a single hot key. |
| 100× · not measured | Partition by team/source once primary CPU constrains writes. Batch durable inbox and per-key writes inside the owning partition. Keep the active alert and its outbox in one transactional boundary. Separate analytical reads from primary writes. Add failover and verify replay after promotion. | Exercise skewed tenants, config-version propagation, replica lag, provider unavailability and failover. Reconcile event identities, logical notifications and receiver receipts separately. |
| 1,000× · not measured | Isolate groups of tenants in independently operated database/broker shards. Preserve ownership of a group during migration. Within a single indivisible hot group, combine updates before the ordered writer. | Rehearse replay and shard moves under traffic. Measure cross-tenant interference, worst-shard queue age and receiver tail latency during failover, together with operator effort and cost. |

## Next delivery optimization

**Deferred after end-to-end verification.** Keep the #36 delivery worker. Candidate `1891028` combined fenced result writes, grouped ACK checks for rows with send slots, skipped writes for healthy channels without a breaker row, and raised the worker's pool limit to at least its channel concurrency. All #36 behavior tests passed. The full local suite reported 589 passed and four live RabbitMQ tests skipped.

One matched Apple M4 Pro trial offered 700 normal and 2 critical events/s across 1,000 normal keys. Both arms used the same generated input and measurement scripts, with two ingest workers, four normal rule workers and one delivery worker. The candidate processed 42,120 events. Both arms verified all 2,000 normal and 240 critical receipts, had zero duplicate receipts and ended with no backlog. [W0 #36 evidence](https://github.com/yy31104/warsaw-hackathon-2026/pull/36#pullrequestreview-6002566130) · [Paired measurement record](../loadtest/results/w2-delivery-finalize-1891028.json)

| End-to-end measurement | #36 | Candidate |
| --- | ---: | ---: |
| Normal receiver p99, target <5 s | 5.738 s | **9.138 s · FAIL** |
| Critical receiver p99, target <1 s | 0.356 s | 0.372 s · PASS |
| Pending delivery peak | 1,575 | 1,800 |
| Peak age of oldest pending notification | 4.770 s | 8.684 s |

The paired 2,000-row local micro-benchmark used the same 5 ms simulated provider and connection-pool size in both arms. Mixed-priority throughput rose from 605.94 to 819.92 deliveries/s with one worker and from 677.12 to 1,086.96 deliveries/s with four. The micro-benchmark and the end-to-end trial produced different outcomes. For the slowest 20 end-to-end normal receipts, median event-processed to first-claim time rose from 5.476 to 8.920 s. Median claim-to-receipt time was 19.8 and 16.7 ms respectively. The trial did not measure application pool checkout wait, and a single candidate run cannot isolate the cause of the pre-claim slowdown. [Paired measurement record](../loadtest/results/w2-delivery-finalize-1891028.json)

**Next measurement.** Compare #36 and the candidate on the same host with alternating run order and fresh volumes. Time claim transactions, team-lock misses and sleeps, application pool checkout, and receipt-to-finalization during the first ten seconds. Compare batched result writes and the pool change independently if the slowdown repeats. Require normal p99 below 5 s, critical p99 below 1 s, equal unique receipt counts and the #36 regression suite before adopting the change.
