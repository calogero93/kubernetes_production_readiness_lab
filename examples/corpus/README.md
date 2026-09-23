# Milestone 1 reference corpus

The chart archives are the exact inputs used for the first public-product corpus.
See [the verified results](results.md) and the saved evidence bundles in
[`evidence/`](evidence/).
Run `./run.sh static` to create four static bundles, including the default and
local kube-prometheus-stack configurations. Run `./run.sh runtime` on a machine
with Docker to create the three runtime bundles. Both commands verify
`SHA256SUMS` before evaluation. Chart
versions are cert-manager `v1.21.2`, Argo CD `10.9.1`, and
kube-prometheus-stack `91.4.1`. The upstream OCI chart digests are:

| Chart | OCI digest |
|---|---|
| cert-manager | `sha256:634dce9c13b56677a2c05e2ab76c312d0be2664022d5dd05815da67e1fd5f610` |
| Argo CD | `sha256:3fe79d7b017587656cb1aa5b92fba44e0e511c40de8b28e68775c401d7e6e43e` |
| kube-prometheus-stack | `sha256:e1b65a9105560251ad78b2ef95ad806245e7311a989b60bdbb315c3878bc21d9` |

Run each chart with `kubeproof inspect charts/<archive>.tgz --profile
../profiles/enterprise-strict.yaml --output <unique-directory>`. Add
`--execute-known-chart` only on a developer-owned machine with Docker, kind,
kubectl and Helm installed. It creates and destroys a fresh kind cluster.

The default kube-prometheus-stack render requests host access through node
exporter and must remain `static_only`. Run it again with
`--values kube-prometheus-stack-local.yaml` to remove that component for a
separate runtime evaluation. The two runs represent different inputs and must
remain separate bundles.

For cert-manager on a fresh cluster, pass `--values cert-manager-kind.yaml`.
The default chart leaves CRD installation disabled; without these CRDs its
startup API check hook cannot complete.

The chart archives pin templates, but some rendered image references may still
use mutable tags. Runtime re-execution can also differ with cluster version,
image registry availability, scheduling and observation time. A static finding
has the same meaning only for the same archive, values, profile, Helm version
and Kubernetes rendering capabilities.
