# First real CPU experiment from the UI

This is a working **local fixture experiment**, not a benchmark of arbitrary
applications or a production-capacity recommendation. The uploaded file is a
packaged Helm chart (`.tgz`). KubeProof renders it, checks the company profile
and a narrow fixture contract, asks for approval, then creates a disposable
kind cluster and runs bounded HTTP load against one named fixture Pod. The
result is stored as a sealed evidence bundle in local history.

## Reading the implementation

Start with `CpuLiveExperiment` in `execution/cpu_live.py`: its four operations
are `validate_resources`, `prepare`, `run` and report generation. Follow only
the boundary you are investigating:

- `execution/cpu_fixture.py` validates the uploaded chart's rendered resources;
  it has no Docker or Kubernetes side effects.
- `execution/cpu_worker.py` builds the reviewed image, contacts the selected
  Pod, sends load and collects the raw measurements.
- `intelligence/workflow.py` validates and schedules plan tasks; the supervisor
  proposes a plan but cannot authorize or execute it.
- `interfaces/live_jobs.py` owns UI preflight, exact chart approval and job
  status; `execution/runtime.py` owns the disposable cluster lifecycle,
  `runtime_monitor.py` owns background sampling/safety, and
  `runtime_checks.py` owns runtime check measurements.
- `intelligence/model_config.py` selects the local or API model; the model
  adapter only converts its answer into a typed plan.
- `frontend/src/liveRequest.ts` builds the confirmed request shown by
  `frontend/src/components/LiveRunReview.tsx` before approval.

This division is by responsibility, not by framework. The deterministic pilot
and the AI supervisor use the same admitted worker and evidence path. The pilot
executes its single trial directly; only AI mode enters the planning graph.
For a symptom-oriented reading order, see the [code map](code-map.md).

## Prepare

Install Docker, kind, Helm, kubectl, Node.js and `uv`. Docker must be running.
From the repository root:

```bash
uv sync --extra ai --extra dev
helm package examples/charts/cpu-fixture --destination /tmp
cd frontend
npm ci
npm run build
cd ..
uv run --extra ai kubeproof serve --data-dir ./.kubeproof
```

Open `http://127.0.0.1:8000` and choose **New CPU run**. The backend must bind
to loopback; live execution is disabled on non-loopback binds. Upload the
packaged `cpu-fixture-0.1.0.tgz` (in `/tmp` in the example). Confirm the CPU
goal, work iterations and company profile. The default profile is a local
example, not your organization's policy. Click **Review chart and requirements**.
This step only renders and evaluates the chart; it does **not** create a cluster
or send load.

Review the chart digest, static admission and findings. A chart whose rendered
Deployment/Service does not match the reviewed fixture contract is static-only
in this UI. The contract permits one to three replicas, resource request/limit
changes and the confirmed work-iteration value; it fixes the local image,
container command and security context. This is intentional: a generic chart
needs an explicitly defined workload endpoint, health check, load semantics and
authorization policy before real load is safe or interpretable.

If the preflight allows execution, tick the approval box and click **Approve and
start real load**. This starts Docker image build, kind cluster creation,
Metrics Server installation, Helm install and the fixed-rate workload. KubeProof
uses `kubectl port-forward` to **one selected Pod** and validates every `/work`
response against the confirmed iteration count. The local pilot schedules one
30-second trial at the target request rate; the UI also supports a configured AI
supervisor for a bounded sequence of CPU trials. Each valid live trial schedules
20–90 seconds of load, at most 200 requests/s and 10,000 requests, with at most
16 requests in flight. The runtime safety monitor can stop a run; the cluster is
deleted after the evaluation, including on most failure paths.
The displayed elapsed-time budget applies to planning and trials after
installation; cluster creation and Helm readiness have separate bounded
timeouts.

The UI polls the run, then opens its evaluation. Read `runtime.cpu_load` for the
goal assessment and the structured CPU trial observation for achieved throughput,
p95 latency, failed requests and sampled Pod CPU. `runtime.resources` separately
applies the company profile's sampled-resource limits. Check the environment
cleanup result and verify the sealed bundle in local history. A completed pilot
that misses the goal is **inconclusive**, not proof that no configuration works;
five valid distinct trials are required for `not_met_within_budget`. Missing
Metrics API samples or endpoint reachability are infrastructure failures, not
negative performance findings. The experiment artifact records the confirmed
request, plan, trial records, raw client summary and CPU samples.

The load path bypasses Service balancing, so two replicas do **not** mean the
Service's aggregate capacity was tested. Kubernetes Metrics API samples are
windowed and indicative, not instantaneous CPU peaks. The image currently uses
a mutable base tag, and the first UI flow has no durable job resume or multi-user
authentication. Run it only on a trusted local machine. No model is required for
the pilot; for AI mode configure and validate a provider first, for example via
[`ai doctor`](../v0.1/milestone-3-ai-design.md#local-llamacpp-path). Model choice
is dynamic through environment variables, never hard-coded.
