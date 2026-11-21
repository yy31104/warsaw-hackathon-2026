# Decision stability evaluation

Source commit `cb87120d1055eb9010a1800c9b22a67c3a870aee`.

| Split | Method | Mean questions | Routing correct | Rule matches correct | Critical misses | INCOMPLETE | Errors | p50 ms | p95 ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| dev | ask_all | 2.25 | 8/8 | 8/8 | 0/2 | 0 | 0 | 0.453 | 1.918 |
| dev | adaptive | 1.25 | 7/8 | 7/8 | 0/2 | 1 | 0 | 0.776 | 1.544 |
| held-out | ask_all | 2.75 | 4/4 | 4/4 | 0/2 | 0 | 0 | 0.486 | 0.760 |
| held-out | adaptive | 1.00 | 4/4 | 4/4 | 0/2 | 0 | 0 | 1.383 | 1.713 |

## Held-out targets

- Lower mean question count than ask-all — PASS
- All four cases routed correctly — PASS
- Zero critical misses — PASS

## Per-case questions

| Split | Case | Ask-all | Adaptive asked | Adaptive charged | Adaptive status |
|---|---|---:|---:|---:|---|
| dev | dev-notional-in-major-units | 2 | 1 | 1 | VARIANT |
| dev | dev-notional-in-minor-units | 2 | 1 | 1 | VARIANT |
| dev | dev-settlement-independent-fields | 2 | 0 | 0 | INVARIANT |
| dev | dev-monitor-service-alias | 2 | 1 | 1 | VARIANT |
| dev | dev-warning-grade | 2 | 1 | 1 | VARIANT |
| dev | dev-pricing-digest | 2 | 1 | 1 | VARIANT |
| dev | dev-timezone-contact-window | 2 | 1 | 1 | VARIANT |
| dev | dev-world-cap | 4 | 0 | 4 | INCOMPLETE |
| held-out | final-monitor-ownership | 3 | 2 | 2 | VARIANT |
| held-out | final-two-rejected-blocks | 2 | 1 | 1 | VARIANT |
| held-out | final-staging-dry-run | 3 | 0 | 0 | INVARIANT |
| held-out | final-settlement-page | 3 | 1 | 1 | VARIANT |

Each case has one ground-truth interpretation. Ask-all reveals every answer and evaluates that single world. Adaptive follows only the oracle's branch of the engine's minimax tree.
Routing requires exact immediate recipient/channel pairs on every sample in a case. Rule matches are scored separately. A labelled critical case misses if any protected sample lacks a critical alert or an expected delivery.
Unresolved cases remain in the correctness and critical denominators. Their question charge is every unknown. The case table also shows the actual number asked.
Each case runs ask-all first. Runtime is one local monotonic measurement per case and method, including analysis and oracle traversal, excluding setup, scoring and file I/O. The percentiles use nearest rank.
