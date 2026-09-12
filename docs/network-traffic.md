# Network traffic diagnostics

On the Smart Home Solutions Energy integration entry in **Settings → Devices &
services**, open its menu and choose **Download diagnostics**. The
`network_traffic` section contains counters since the current API client was
created (normally integration load). Reloading/restarting resets them; downloading
does not. `integration_version` identifies the installed build.

Each endpoint has request counts, failed requests, HTTP errors, responses without
an HTTP status, decoded response-body bytes, estimated outgoing JSON-body bytes,
unmeasured-body counts and cumulative elapsed milliseconds. HTTP and contract
validation failures are counted too. A timeout reading the response is marked
unmeasured rather than treated as an empty successful response.

No plan, configuration, token, home ID, query value or response body is included.
Counters stay in memory, have bounded endpoint cardinality, and are not uploaded.
All API calls through `ShsApiClient` are covered, including pairing on its own
client; pre-setup pairing counters are not carried into the integration client.
HA's other integrations and browser frontend traffic are outside this report.

These are application payload sizes, **not billed Supabase egress**. Responses
are counted after decompression; headers and transport are excluded. JSON
request bytes are estimates using the standard serializer. Start with endpoints
having the largest response totals, then compare requests and average bytes per
measured response over equal observation windows. Two downloads from the same
running integration can be subtracted to measure a specific interval.

HA's incoming responses also appear in the backend's outgoing counters, so do
not sum them together. Use Supabase's usage totals to validate the billing impact.
The website/backend repository documents its complementary browser downloads
and structured Edge Function logs in `docs/network-traffic.md`.
