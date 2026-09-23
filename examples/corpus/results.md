# Milestone 1 verification

Verified on 2026-09-22 with the pinned archives in this directory and the
`enterprise-strict` profile. The saved bundles are in [`evidence/`](evidence/).
Each archive contains `evaluation.json`, `report.md`, the normalized profile,
the rendered manifest, and collected artifacts. Extract an archive with
`tar -xzf evidence/<name>.tar.gz -C <destination>`. Verify saved archive
integrity with `cd evidence && sha256sum -c SHA256SUMS`.

## Static corpus

Run `./run.sh static <output-directory>` to reproduce these four evaluations.
The exit code of an individual inspection is `2` when it finds profile blockers;
the corpus script accepts that expected result.

| Chart and values | Admission | Blockers | Warnings | Saved bundle |
|---|---|---:|---:|---|
| cert-manager default | `admit` | 19 | 0 | `static.tar.gz` |
| Argo CD default | `admit` | 48 | 4 | `static.tar.gz` |
| kube-prometheus-stack default | `static_only` | 42 | 104 | `static.tar.gz`, `kube-prometheus-stack-default.tar.gz` |
| kube-prometheus-stack local values | `admit` | 33 | 104 | `static.tar.gz` |

The default kube-prometheus-stack render matched safety rules `KP-SAFE-002`,
`KP-SAFE-003`, and `KP-SAFE-005`; no runtime evaluation was attempted for that
input. The local values disable node exporter, producing a distinct admissible
input. Every completed static check has at least one observation. A second
cert-manager static run produced identical admission, checks, findings,
observations, and rendered-manifest SHA-256. The two input records differed only
in their relative versus absolute spelling of the chart path.

## Runtime corpus

Run `./run.sh runtime <output-directory>` on a developer-owned Docker host to
reproduce the three fresh-kind evaluations. `pass` below refers to the runtime
check; blockers are separate, deterministic violations of the strict profile.

| Chart and values | Install and readiness | Metrics samples | Pod replacement | Observed DNS | Blockers / warnings | Bundle |
|---|---|---|---|---|---:|---|
| cert-manager, kind CRDs | pass | pass | pass | inconclusive | 19 / 52 | `cert-manager-kind.tar.gz` |
| Argo CD default | pass | pass | pass | inconclusive | 48 / 4 | `argo-cd.tar.gz` |
| kube-prometheus-stack local values | pass | pass | pass | warning | 33 / 106 | `kube-prometheus-stack-local.tar.gz` |

The cert-manager evaluation is `2a713f04-b4de-4e0e-879e-8c9bfd864e1d`,
Argo CD is `d4f8da47-9d9b-40e1-b8e9-e620358f2ab7`, and
kube-prometheus-stack is `d13f4d4e-4b2f-4b48-a347-5be89c6d5193`.
All three report successful release and cluster cleanup with no leftovers.
`kind get clusters` reported no remaining clusters after verification.

DNS is inconclusive where the observation window did not establish an external
dependency. In the local kube-prometheus-stack run, CoreDNS recorded queries for
`grafana.com` and `storage.googleapis.com`, attributed to Grafana; the finding
is a warning about observed queries, not proof that the product requires those
services to function.

## Environment and limits

This run used Python 3.12, Helm 3.21.1, kind 0.23.0, kubectl 1.34.1, Docker
client/server 29.4.0, and the packaged Metrics Server v0.7.2 manifest. The
chart package hashes are in [`SHA256SUMS`](SHA256SUMS). Kind, image registry,
Kubernetes version, and timing can affect runtime observations. The bundles
describe one local run, not a guarantee that every future run has the same
readiness times or DNS traffic.

Isolated tests also cover Docker creation failure, image-pull infrastructure
failure, product install failure, dynamic unsafe Pod cancellation, and cleanup
failure. In the infrastructure cases, product checks remain untested rather
than producing a product finding.
