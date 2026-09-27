# Exploratory CPU-fixture calibration — 2026-09-25

This is a local **exploratory** run, not a company SLO, Kubernetes benchmark,
autoscaling result, or production-capacity claim. Its purpose is to find a
workload and offered request rate that make a later, predeclared Helm-values
comparison informative.

## Setup

- Workload: `kubeproof.benchmarks.cpu_http.app`, single HTTP worker, fixed PBKDF2
  work per `GET /work` request; image `kubeproof-cpu-fixture:local` with local
  image ID `sha256:c5db6fc5f117809f07b359b1fad973e52fc6853961dec500066d8d380aa9b8cd`.
- Base image resolved locally to
  `python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36`.
  The Dockerfile uses a mutable tag; the resolved digest must be pinned before
  producing comparable final evidence.
- Docker Engine 29.4.0, Linux x86_64. One container with `--cpus=0.5` and
  `--memory=128m`, published only on `127.0.0.1` at an ephemeral host port.
- Driver: `kubeproof.benchmarks.cpu_http.load` on the same host, fixed-rate
  arrivals, 16 maximum in-flight requests, 2-second request timeout. Each point
  scheduled ten seconds of arrivals: request count = target requests/s × 10.
- One run per point; there are no independent repeats or confidence intervals.

## Observations

`p50` and `p95` are milliseconds among **successful** responses. A client drop
is a scheduled request that was not sent because the driver's in-flight bound
was full; it is not a server response.

| CPU iterations/request | Target req/s | Successful / planned | Client drops | Network errors | Successful req/s | p50 ms | p95 ms | Max schedule lag ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 20,000 | 10 | 100 / 100 | 0 | 0 | 10.000 | 3.205 | 4.387 | 3.311 |
| 20,000 | 100 | 1,000 / 1,000 | 0 | 0 | 100.000 | 2.986 | 3.811 | 0.655 |
| 20,000 | 200 | 2,000 / 2,000 | 0 | 0 | 200.000 | 2.847 | 3.509 | 0.642 |
| 100,000 | 20 | 200 / 200 | 0 | 0 | 20.000 | 11.161 | 12.243 | 5.107 |
| 100,000 | 40 | 400 / 400 | 0 | 0 | 40.000 | 11.112 | 12.407 | 0.987 |
| 100,000 | 50 | 500 / 500 | 0 | 0 | 49.717 | 48.617 | 83.886 | 0.888 |
| 100,000 | 70 | 40 / 700 | 587 | 73 | 3.455 | 93.155 | 1,943.391 | 1.008 |

One `docker stats --no-stream` snapshot during the 100,000-iteration,
50-request/s run showed about 50.83% CPU and 14.44 MiB / 128 MiB memory. It is
an indicative snapshot, **not** a CPU time series or a peak measurement.

## Interpretation and next decision

- At 20,000 iterations, even 200 requests/s did not visibly degrade this
  setup, so that workload is too light for the first configuration comparison.
- At 100,000 iterations, 40 requests/s was stable in one run while 50 requests/s
  produced much higher latency near the container's CPU limit. This makes
  50 requests/s a useful **candidate for one 0.5-CPU worker**. It is not yet
  an adopted target or SLO for the Helm chart, which defaults to two replicas;
  that Service-level target needs its own in-cluster calibration.
- The 70-request/s point is a negative result: client saturation, timeouts and
  only 40 verified responses make its successful-response p95 unsuitable as a
  clean server-capacity estimate. It still shows that the combined setup could
  not deliver the offered load.

Before comparing any Helm values, choose and record the fixed iteration count,
target request rate, p95 threshold, allowed error/drop budget, run duration and
number of repetitions. The user owns that decision. The comparison must also
use a load path that reaches all replicas: [`kubectl port-forward svc/...`
selects one Pod](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_port-forward/),
so it cannot measure Service-level horizontal scaling. This calibration
used a single Docker container, not Kubernetes, HPA, VPA or KubeProof runtime
collection.
