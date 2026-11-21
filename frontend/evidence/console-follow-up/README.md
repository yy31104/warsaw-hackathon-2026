# Console follow-up verification

The before screenshots came from the merged console in #35 (`b84bd4e`). The after screenshots use the production `VITE_MOCK=0` frontend on fresh Compose projects with synthetic inputs and Assist disabled.

| Screen | Before | After |
|---|---|---|
| Timeline | [Separate raw notification and attempt rows](before/incident.jpg) | [One readable row per notification](after/incident.jpg) |
| Sources | [Generated ID, free-text recipients and empty quiet sample](before/sources.jpg) | [Named schema, person and team selected, and the deciding value selected in the copied sample](after/sources-quiet-test.jpg) |
| Now | [Benchmark tile](before/now.jpg) | [Benchmark tile and fallback message](after/now.jpg) · [Four tiles from the response API](after/now-response-live.jpg) |

On the main-based test stack at `20e61d9`, `GET /v1/stats/response` returned `404`. The four-tile screenshot uses the same frontend with Yi's response API at `db5673b`. The browser compared each displayed value with the API response. A separate browser context exercised a null metric with a supplied response.

The Timeline check compared the call duration with its successful delivery attempt and verified that the raw decision and attempt were still accessible from **More details**. Sources selected Rita and the Rates team and changed only the amount in the quiet sample. The next event routed after the named schema was published with its generated ID.

Use the [console reproduction commands](../../CONSOLE_VALIDATION.md#reproduce) with a fresh project. The run covered the console on desktop and phone in both themes. [Main-based browser results](result.json) · [Response API browser results](response-api-result.json)
