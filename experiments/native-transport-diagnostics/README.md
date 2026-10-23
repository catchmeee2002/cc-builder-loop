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
