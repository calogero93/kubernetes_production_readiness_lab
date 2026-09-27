# CPU HTTP benchmark fixture

This is a controlled workload for the **exploratory calibration phase** of the
first performance experiment. It is not an AI component and does not recommend
Helm values or an autoscaler.

`GET /work` performs the same bounded PBKDF2 CPU work on every request and
returns a digest that the load driver checks. `GET /healthz` does no benchmark
work. The server handles one request at a time, making CPU contention visible
as throughput loss and increased response latency. The number of iterations is
part of the workload definition; keep it unchanged between configurations.

## Run locally, without Docker

From the repository root, use two terminals:

```bash
uv run python -m kubeproof.benchmarks.cpu_http.app --iterations 20000
```

```bash
uv run python -m kubeproof.benchmarks.cpu_http.load \
  --url http://127.0.0.1:8080/work \
  --rate 5 --requests 50 --iterations 20000
```

The second command schedules 50 arrivals at 5 requests/second and prints JSON.
Change `--rate` in separate exploratory runs to find an informative load. The
driver accepts only an explicit loopback `/work` URL, caps the rate, request
count, concurrency and timeout, and counts requests it could not send as
`dropped_by_client`. A drop indicates the **client** was saturated; do not
interpret it as a successful server response. `success_rps` counts verified
responses over at least the intended test duration; latency percentiles use
successful responses only. Errors and unsent requests remain visible separately.
`max_schedule_lag_ms` exposes a slow client scheduler; a large value weakens
the claim that the intended arrival rhythm was actually applied.

Real-time scheduling and measured latency are not byte-for-byte deterministic.
The *intended* arrival schedule and CPU work are fixed, while the outcomes may
vary with host load. This local run does not measure per-Pod CPU, Kubernetes
autoscaling, or a production service-level objective.

## Kubernetes fixture prepared for later comparison

`Dockerfile` builds the same service for a container, and
[`examples/charts/cpu-fixture`](../../../../examples/charts/cpu-fixture) exposes
`replicaCount`, CPU requests and CPU limits as Helm values. The default two
replicas satisfy the example enterprise profile's minimum replica count.
`workIterations` must remain fixed when comparing those values.

The current KubeProof runtime creates its own disposable kind cluster. It does
not yet load this locally built image into that cluster or execute this load
driver as a registered experiment. Consequently, the chart is not yet an
end-to-end `kubeproof inspect --execute-known-chart` benchmark. The Docker
base image tag is also mutable; pin its digest before producing comparative
evidence. No Kubernetes run or HPA/VPA claim should be inferred from local
calibration output.

Do not use `kubectl port-forward svc/...` to compare replica counts: [port-forward
selects one backing Pod](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_port-forward/)
rather than exercising Service load balancing. A later in-cluster driver or a
verified load-balanced endpoint is needed for that test.

After calibration, agree on a target request rate, latency threshold and error
budget **before** comparing Helm values. Preserve the workload definition,
target rate, request count, image digest and environment for every candidate.
The first local exploratory results are recorded in the
[2026-09-25 calibration note](../../../../docs/experiments/cpu-calibration-2026-09-25.md).
