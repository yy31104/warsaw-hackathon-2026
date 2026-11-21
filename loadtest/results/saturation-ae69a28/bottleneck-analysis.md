The limiting stage depended on the workload. Distributed alert keys filled the PostgreSQL rule inbox. At the highest single-key offer, a second RabbitMQ intake consumer cut the broker's late-window queue median from 99,609 to 158 messages and moved the growing backlog into the rule inbox.

### Rule processing with 1,000 keys

At 1,400 normal events/s, RabbitMQ confirmed all 84,120 scheduled events inside the offer for each worker count, and the median broker queue was zero throughout. With one normal rule worker, sustained processing was 626.0 events/s, and the PostgreSQL inbox medians rose from 15,790 to 29,540 to 41,995. Scaling to four workers raised processing to 1,405.9 events/s, with inbox medians of 410, 350.5 and 384. Mean PostgreSQL CPU rose from 78.1% to 166.1%. At 2,800 normal events/s, even four workers saw inbox medians rise from 20,101 to 39,648 to 56,041 while the RabbitMQ medians stayed below 102. [Matrix run summaries](batch.json)

A fresh one-versus-four comparison at 1,400 normal events/s used the same workload hash. It reproduced 630.0 and 1,404.8 sustained events/s, respectively. Late-window inbox medians were 41,106 and 350.5. All 84,120 event submissions were broker-confirmed in both runs. [Rule worker repeat](diagnostic-rules/batch.json)

### Broker intake at the highest single-key offer

With four normal rule workers and a target of 5,600 normal events/s, we repeated the same scheduled workload with one and two RabbitMQ intake consumers. RabbitMQ confirmed 330,570 and 332,880 events inside the offer window, respectively. Ingested throughput rose from 3,571.0 to 5,546.8 events/s. RabbitMQ queue medians went from 32,442 / 65,948 / 99,608.5 with one intake consumer to 105 / 0 / 157.5 with two. With the second consumer, the pending rule inbox grew from a median of 29,120 to 88,160.5, and sustained processing reached 3,782.0 events/s. The queue change locates the first limit in the RabbitMQ-to-inbox path and reveals the next limit in rule processing. [One intake consumer](diagnostic-intake1/batch.json) · [Two intake consumers](diagnostic-intake2/batch.json)

Critical receiver p99 moved from 28,449 ms (N=206) to 1,233 ms (N=238) in those runs. Each run targeted 120 critical events and expected 240 SMS and phone receipts. Observations ended after a bounded drain window. Mean PostgreSQL CPU rose from 107.6% to 139.7%, and sampled active advisory-lock waits rose from 8 to 69. The samples show more recorded lock contention alongside the inbox backlog. Query-level profiling would identify which statements account for that time. [Intake comparison and lock observations](diagnostic-intake2/m11-11210202-ea57d-r5600-k1-w4/samples.json)

### Notification delivery after the alert is created

At 700 normal events/s across 1,000 keys with four rule workers, we changed the delivery worker count from one to four and kept the scheduled workload hash. The rule inbox stayed steady in both runs, with 701.3 and 702.7 sustained events/s. Normal receiver p99 improved from 39,877 to 33,627 ms (N=2,000 each). The middle-block delivery queue median went from 407.5 to 53. Peak delivery work remained close to 1,900 items as the first alerts arrived in a burst. All 2,240 expected simulated receipts arrived in each run. [One delivery worker](diagnostic-delivery1/batch.json) · [Four delivery workers](diagnostic-delivery4/batch.json)
