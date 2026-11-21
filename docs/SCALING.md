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

**Intel matrix, 1,000 keys, 5,600 normal events/s.** Critical receiver p99 with **two intake consumers: not measured on Intel**. The Apple pair below measures this combination separately. The matrix used one intake consumer: critical p99 was 27.437 s (208 received / 240 expected), 28.604 s (202 / 240) and 34.156 s (180 / 240) with one, two and four normal rule workers, respectively. [Matrix configurations and receipts](../loadtest/results/saturation-ae69a28/runs.csv)

**1,000 keys, 2,800 normal events/s.** With one intake consumer and four normal rule workers, the rule inbox grew to a late-window median of **56,040.5 pending events** while critical receiver p99 stayed at **0.443 s** (240 received / 240 expected). In this run the critical lane was isolated from the normal rule backlog. [Backlog and receiver measurements](../loadtest/results/saturation-ae69a28/m11-11210120-1d623-r2800-k1000-w4/summary.json)

## Delivery and loss accounting

With 1,000 distinct normal alerts routed to one recipient, normal receiver p99 spanned **36.394–61.134 s** (2,000 received / 2,000 expected in each trial). In the controlled 700/s repeat, increasing delivery workers from one to four reduced p99 from **39.877 s** (2,000 received / 2,000 expected) to **33.627 s** (2,000 / 2,000) and the middle-block delivery backlog from **407.5 to 53**, while the rule inbox stayed steady. Delivery and recipient fan-out deserve separate capacity measurements from event processing. [Normal receiver series](../loadtest/results/saturation-ae69a28/runs.csv) · [One delivery worker](../loadtest/results/saturation-ae69a28/diagnostic-delivery1/batch.json) · [Four delivery workers](../loadtest/results/saturation-ae69a28/diagnostic-delivery4/batch.json)

Of **3,782,880** scheduled events, RabbitMQ confirmed **3,775,341**. The difference of **7,539** was dropped at the generator before broker acceptance. The publisher recorded **zero errors** and ingestion recorded **zero rejections**. The bounded observation can leave broker-confirmed events and expected receiver receipts pending. A claim of permanent downstream loss or lossless eventual delivery would require following those IDs to terminal state. [Aggregate counts](../loadtest/results/saturation-ae69a28/REPORT.md) · [Per-run reconciliation and pending counts](../loadtest/results/saturation-ae69a28/runs.csv)

### Delivery batching · end-to-end

Apple M4 Pro, main `d7a6884` before and PR #36 `cc5bd7c` after. Each run offered 700 normal plus 2 critical events/s across 1,000 normal keys for 60 s, followed by up to 20 s of drain. Two AMQP consumers, four normal rule workers, one critical rule worker and one delivery worker ran in each trial. Both used workload SHA-256 `5804a2c054511b70324d863286d262bcbb02b3c8949b1a6feee529841dc5b4be`. Provider receipts are verified by ID, channel, recipient and generation time through fake-telecom. Other local containers remained available. [Pre-registered target](./SCORECARD.md) · [Machine and Docker inventory](../loadtest/results/w2-delivery-e2e-cc5bd7c/host.json) · [Before · main](../loadtest/results/w2-delivery-e2e-cc5bd7c/before/m11-11210324-24866-r700-k1000-w4/summary.json) · [After · #36](../loadtest/results/w2-delivery-e2e-cc5bd7c/after/m11-11210326-bb859-r700-k1000-w4/summary.json)

| Measurement | Before · main | After · #36 |
| --- | ---: | ---: |
| Normal p50 / p95 / p99 (ms), received / expected | 17969 / 26922 / 27424 (2,000 / 2,000) | 3292 / 5577 / 5738 (2,000 / 2,000) |
| Critical p50 / p95 / p99 (ms), received / expected | 195 / 336 / 547 (240 / 240) | 219 / 317 / 356 (240 / 240) |
| Delivery pending · early / middle / late median | 1,033 / 0.0 / 0.0 | 0 / 0.0 / 0.0 |
| Verified sends/s over the 60 s offer | 37.30 | 37.30 |
| Verified sends/s over the offer and any receipt drain | 37.21 | 37.20 |
| Verified sends/s during the active receiver span | 37.42 | 37.37 |
| PostgreSQL mean / peak CPU (one core = 100%) | 94.8% / 146.1% (N=12) | 63.1% / 121.1% (N=13) |
| Offered / generator-not-submitted / confirmed | 42,120 / 0 / 42,120 | 42,120 / 0 / 42,120 |
| Accepted events pending processing at cutoff | 0 | 0 |

The active receiver span divides the inter-receipt count by elapsed time from the first to the last verified receipt. Early, middle and late queue medians use the three steady blocks after the initial 10 s. Missing receipts have no latency sample. [Send-rate denominators](../loadtest/results/w2-delivery-e2e-cc5bd7c/w0-sends.json) · [Before · main](../loadtest/results/w2-delivery-e2e-cc5bd7c/before/m11-11210324-24866-r700-k1000-w4/summary.json) · [After · #36](../loadtest/results/w2-delivery-e2e-cc5bd7c/after/m11-11210326-bb859-r700-k1000-w4/summary.json)

Pre-registered targets on the after trial. Normal p99 <5 s **FAIL**. Critical p99 <1 s **PASS**. Complete unique receiver coverage **PASS**. [After · #36](../loadtest/results/w2-delivery-e2e-cc5bd7c/after/m11-11210326-bb859-r700-k1000-w4/summary.json)

### Delivery batching · micro-benchmark

The W2 micro-benchmark timed a pre-populated outbox on the same Mac with a 5 ms in-process SMS provider. Its mixed workload was one team and 2,000 due notifications. This measures the delivery worker and the database, with every notification available at the start. See the separate end-to-end trial above for the queue, rules and receiver path. [W2 method and raw results](https://github.com/yy31104/warsaw-hackathon-2026/pull/36)

| Delivery workers | Measured sends/s, baseline → batch | Normal p99, received / expected, baseline → batch | Critical p99, received / expected, baseline → batch |
| ---: | ---: | --- | --- |
| 1 | 58.83 → 691.72 | 33.708 s (1,960/1,960) → 2.857 s (1,960/1,960) | 0.711 s (40/40) → 0.089 s (40/40) |
| 4 | 82.41 → 749.20 | 24.036 s (1,960/1,960) → 2.650 s (1,960/1,960) | 0.450 s (40/40) → 0.094 s (40/40) |

## Apple M4 Pro cross-machine check

On the second host, one normal rule worker processed a 60-second, 1,000-key offer. Each row reports throughput across the **entire offer window** and latency to the mock SMS/phone receiver. CPU is mean PostgreSQL usage as a percentage of one core. All accepted events and expected receipts reconciled after drain in both trials. The Mac used a later source commit, omitted critical traffic, and had desktop apps running. These runs independently check which queue grows. They cannot establish a hardware speed ratio or critical latency. [Mac configuration](../loadtest/results/saturation-10c398e-m4-pro/machine.json) · [Generator command](../loadtest/results/saturation-10c398e-m4-pro/k1000-w1-r700/run.json)

| Offered/s | Processed/s | Receiver p50 / p95 / p99 (s), received / expected | Inbox backlog at 15 / 30 / 45 / 60 s | Generator drops | PG CPU | Evidence |
| ---: | ---: | --- | --- | ---: | ---: | --- |
| 700 | 698.25 | 16.034 / 24.275 / 24.748 (2,000 / 2,000) | 140 / 210 / 175 / 105 | 0 | 77.18% | [Counters and receipt reconciliation](../loadtest/results/saturation-10c398e-m4-pro/k1000-w1-r700/metrics.json) |
| 1,400 | 1,007.83 | 15.165 / 24.906 / 25.704 (2,000 / 2,000) | 6,130 / 12,530 / 18,330 / 23,530 | 0 | 96.68% | [Counters and receipt reconciliation](../loadtest/results/saturation-10c398e-m4-pro/k1000-w1-r1400/metrics.json) |

## 1,000 keys at 5,600/s, 1 vs 2 intake consumers (Apple M4 Pro)

The same Apple M4 Pro ran both trials on `d7a6884813f7`, using the same images and clean volumes. Each offered 5,600 normal and two critical events/s for 60 s, with four normal rule workers, one critical rule worker and one delivery worker. The comparison used explicit Compose replica counts. Both input schedules have SHA-256 `c61d01984d685038242e040c6af641541675b224807cdf445acfeef05c9fc490`. The CPU row uses 100% per logical CPU. [Machine and restored inventory](../loadtest/results/intake-pair-d7a6884-m4/host.json) · [Live container samples](../loadtest/results/intake-pair-d7a6884-m4/active-containers.json) · [1 intake](../loadtest/results/intake-pair-d7a6884-m4/intake1/m11-11210251-ea4f7-r5600-k1000-w4/summary.json) · [2 intake](../loadtest/results/intake-pair-d7a6884-m4/intake2/m11-11210252-253d4-r5600-k1000-w4/summary.json)

| Count or latency | One intake | Two intakes |
| --- | ---: | ---: |
| Offered events | 336,120 | 336,120 |
| Generator not submitted | 0 | 14,140 |
| Publish errors | 0 | 0 |
| Broker-confirmed by end of offer | 336,120 | 321,699 |
| Broker-confirmed in total | 336,120 | 321,980 |
| Ingested during offer | 230,930 | 320,060 |
| Processed during offer | 176,882 | 147,195 |
| Processed/s during offer | 2,948 | 2,453.2 |
| Ingested by cutoff | 315,730 | 321,980 |
| Processed by cutoff | 239,912 | 205,996 |
| Pending at cutoff · confirmed, awaiting durable intake | 20,390 | 0 |
| Pending at cutoff · in inbox, awaiting rule processing | 75,818 | 115,984 |
| Pending at cutoff · total confirmed events awaiting processing | 96,208 | 115,984 |
| Rejected inputs | 0 | 0 |
| Mock SMS/phone receipts verified / expected slots | 2,224 / 2,240 | 2,230 / 2,240 |
| Expected receipt slots unverified at cutoff | 16 | 10 |
| Critical receiver p50 / p95 / p99 (ms), received / expected | 12,947 / 22,784 / 23,784 (224 / 240) | 401 / 1,261 / 1,576 (230 / 240) |
| Normal receiver p99 (ms), received / expected | 41,856 (2,000 / 2,000) | 70,115 (2,000 / 2,000) |
| Elapsed offer start to snapshot (s) | 80.229 | 80.367 |
| Mean / peak PostgreSQL CPU, N samples | 246.7% / 269.1% (N=12) | 188.3% / 296.2% (N=13) |

Backlog entries below are early / middle / late medians. The three equal blocks cover the offer after the initial 10 s. Broker-ready counts exclude deliveries in flight to consumers. Receipts represent logical channel notifications and have a different denominator from events. The post-offer drain is capped at 20 s, with actual snapshot times above. Pending IDs at this cutoff have unknown eventual outcomes. [1 intake](../loadtest/results/intake-pair-d7a6884-m4/intake1/m11-11210251-ea4f7-r5600-k1000-w4/summary.json) · [2 intake](../loadtest/results/intake-pair-d7a6884-m4/intake2/m11-11210252-253d4-r5600-k1000-w4/summary.json)

| Queue | One intake | Two intakes |
| --- | ---: | ---: |
| RabbitMQ ready | 27,132 / 60,043 / 89,326 | 160 / 62.5 / 1,782 |
| PostgreSQL rule inbox | 17,017 / 32,682 / 48,059 | 44,805 / 87,702 / 132,465.5 |
| Delivery outbox | 1,348 / 412 / 0 | 1,509 / 968 / 523 |
| Broker ready + rule inbox | 44,149 / 92,725 / 137,385 | 44,805 / 87,764.5 / 134,247.5 |

In this pair, two consumers brought observed critical p99 from 23.784 s (224 received / 240 expected) to 1.576 s (230 / 240) and moved more work into the rule inbox. Normal receiver p99 rose from 41.856 s (2,000 received / 2,000 expected) to 70.115 s (2,000 / 2,000). Both arms replayed the same offered schedule, but the two-consumer arm left 14,140 events unsubmitted at the generator and confirmed fewer events. Completed processing during the offer also fell. This single pair measures the trade-off on this Mac. [1 intake](../loadtest/results/intake-pair-d7a6884-m4/intake1/m11-11210251-ea4f7-r5600-k1000-w4/summary.json) · [2 intake](../loadtest/results/intake-pair-d7a6884-m4/intake2/m11-11210252-253d4-r5600-k1000-w4/summary.json)

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
