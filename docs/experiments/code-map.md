# Code map for the local CPU experiment

This page is a reading and debugging map for the first end-to-end experiment.
The fixture is intentionally narrow: one reviewed Helm chart, one selected Pod,
CPU load, and a disposable local kind cluster. It is not a generic Helm-chart
benchmark.

## Follow one run

1. `frontend/src/components/LiveCpuRun.tsx` gathers the chart and requirements.
   `frontend/src/liveRequest.ts` builds the confirmed request and fixed budgets.
   `LiveRunReview.tsx` shows exactly what must be approved.
2. `interfaces/live_jobs.py` validates the submission, saves the chart in a
   temporary directory, runs static preflight, binds approval to the chart's
   SHA-256 digest, and tracks the background job. Preflight cannot create a
   cluster or generate load.
3. `application/service.py` coordinates chart inspection and evidence-bundle
   creation. `core/analysis.py` performs deterministic static checks; its
   security check separates Pod and container rules.
4. `execution/runtime.py` owns cluster creation, installation and cleanup.
   `runtime_monitor.py` collects/supervises background metrics;
   `runtime_checks.py` contains the resource, recovery and DNS checks. These
   modules do not ask a model to assess product readiness.
5. `execution/cpu_live.py` runs the deterministic pilot directly and adapts
   CPU results into the report. AI mode uses `intelligence/workflow.py` to
   maintain graph state and dispatch admitted tasks. `intelligence/control.py`
   decides task readiness and plan outcomes; `model_adapter.py` converts model
   output into a typed plan. The model proposes tests; it cannot authorize or
   execute them.
6. `execution/cpu_worker.py` builds the fixture image, forwards one Pod,
   generates bounded HTTP load and samples CPU. The resulting observations and
   artifacts are sealed by the evidence layer and indexed in local history.

## Where to start when something fails

| Symptom | First place to inspect |
|---|---|
| Upload or profile rejected | `interfaces/live_jobs.py` validation and `tests/interfaces/test_live_jobs.py` |
| Static admission or fixture contract fails | `core/analysis.py`, `execution/cpu_fixture.py` |
| Model endpoint/configuration fails | `intelligence/model_config.py`, `kubeproof ai doctor` |
| Plan or budget rejected | `intelligence/control.py`, `intelligence/workflow.py` |
| Cluster/install/cleanup fails | `execution/runtime.py`, `runtime_monitor.py` |
| CPU trial has no valid sample | `execution/cpu_worker.py` and the CPU artifact in the bundle |
| UI shows a stale or failed run | `LiveCpuRun.tsx`, `LiveRunReview.tsx`, `interfaces/live_jobs.py` |

Fast regression checks are `uv run pytest -q` and, in `frontend/`, `npm test`
and `npm run build`. The opt-in Docker/kind smoke test is
`KUBEPROOF_RUN_LIVE_SMOKE=1 uv run pytest -q tests/execution/test_live_smoke.py`;
it creates and destroys a local cluster, so run it only on a machine dedicated
to this experiment.
