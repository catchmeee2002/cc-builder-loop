# Native transport diagnostics

Compare complete-frame deadlines without resuming a business run or changing the installed timeout policy.
The report is diagnostic evidence, not Tester acceptance or proof of an upstream root cause.

## Offline comparison

Run from the checkout being examined:

```bash
python3 -B experiments/native-transport-diagnostics/runner.py --output /tmp/transport-offline.json
```

The runner uses local pipes and no model. It compares two scaled deadlines for normal completion, delayed
completion, partial frames, buffered frames, disconnect and total timeout. Delayed completion and partial
frames should fail under the shorter deadline and complete under the longer one. Disconnect and total timeout
remain failures under both. Timing-sensitive failures on a heavily loaded host must be investigated rather
than interpreted as a product incident.

## Live comparison

Run on the incident host only when that diagnostic execution is authorized:

```bash
python3 -B experiments/native-transport-diagnostics/runner.py --live \
  --model MODEL --effort high --output /tmp/transport-live.json
```

An optional `--prompt-file` supplies a no-tools diagnostic task. Its content is represented only by a digest
in the report. Each group starts a new thread in an empty temporary directory with a read-only sandbox;
observed tool activity invalidates the experiment and stops the comparison. Do not supply a business task
requiring tools or writes. The two groups use the same task and requested model/effort, with 120-second and
300-second read deadlines and the same 600-second total deadline. Observed settings are compared before the
second turn. Model turns are not retried. Before the comparison, the existing protocol capability check may create
protocol-only temporary threads and perform its bounded admission retries; it submits no model task.
Version/schema commands are limited to 30 seconds each. Failed cleanup stops the comparison.

The live path records runtime/executable identity, thread/turn identity, observed settings, outcome and
retained diagnostic receipts. It creates diagnostic threads but never opens a business ledger, resumes an
existing thread or promotes results to role evidence. Model output is represented by a digest, not saved text.
The output path must be new. Reports may contain host paths and identifiers; review before sharing.

A longer-deadline completion is evidence that the particular diagnostic task can outlast the shorter deadline.
It does not establish why an earlier Tester failed. If both groups finish quickly, the incident was not
reproduced. Missing upstream request logs remain missing evidence; do not infer network or model causality
from the local `responseStreamTimeout` classification.

## Successful Driver receive observations

On the experimental runtime, set `CODEX_BUILDER_LOOP_OBSERVATION_DIR` to an existing absolute private
directory outside frozen checkouts (owned by the current user, mode 0700) for the Driver process.
Leave it unset for default behavior.
The Driver creates a separate 0600 `transport-*.jsonl` journal for each transport. Keep these files alongside
the upstream capture launcher output: the launcher observes the App Server's upstream side, while this journal
observes actual Driver reads. Do not substitute one for the other.

Verify runtime and executable identity, the journal's transport generation, and the awaited thread/turn.
Inspect `journal_closed.turns[].max_complete_frame_wait_seconds` and `longest_read_frame` for a completed turn.
A value above 120 seconds is a measured complete-frame read wait crossing the former boundary in this run;
it is not a deterministic replay of an earlier run or proof of the upstream cause. The measurement ends before
JSON parsing and excludes host work between reads; partial bytes may arrive during that wait. Do not call it
network-byte silence. A successful short wait does not exercise the old boundary.

Require the closing footer, inspect cleanup and capture warnings, and report `omitted_events` and
`evicted_turn_summaries`. Event storage is capped at 16 MiB; at most 64 completed wait summaries are retained.
Maximum waits remain in retained summaries when detailed events are truncated. Summaries never imply role
acceptance. A killed process or write failure may leave incomplete evidence. Do not rerun an already finalized
business delivery solely to populate this diagnostic channel; use a future task or separately scoped diagnostic.
