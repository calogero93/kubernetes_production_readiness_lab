# Evidence bundle contract (schema 0.2)

An evaluation produces one directory that can be inspected without a cluster:

```text
run/
├── evaluation.json
├── bundle-manifest.json
├── report.md
├── profile.normalized.json
├── input/rendered-manifests.redacted.yaml
└── artifacts/                         # present when runtime collectors emit data
```

`evaluation.json` is the typed result. Each observation has its own schema
version, source class, source reference and, for static input, the SHA-256 digest
of the original rendered manifest. Each check records its experiment version.
The result records the normalized profile digest, ordered values-file and
`--set` digests, local tool/platform versions, and the runtime Kubernetes server
version when available. `--set` values themselves are not stored because they
may contain credentials.

`bundle-manifest.json` lists SHA-256 digests for every other file and for every
serialized observation. It is written after all artifacts, and the whole bundle
is verified before its output directory is published. `kubeproof verify RUN`
checks the manifest, observation seals, source provenance, profile digest and
that `report.md` is exactly the rendering of `evaluation.json`. Importing or
reading a sealed bundle through local history applies the same checks. Extra
files and symbolic links are rejected.

The seal detects an altered file or observation as long as the manifest remains
the original. It is not a digital signature: someone able to replace both data
and manifest can create a new internally consistent bundle. Authenticity or
publication would require a trusted signature or externally pinned manifest
digest. The raw rendered manifest is redacted in the bundle, so its original
digest identifies the source but cannot be recomputed from the redacted copy.
The chart package and values files are identified by digests but are not copied
into the bundle; reproducing the run requires access to those exact inputs.
Runtime observation provenance identifies the producing check and cluster, but
does not always identify a specific raw Kubernetes API response. More granular
artifact references remain a future audit enhancement.

Schema 0.1 bundles remain importable for local history compatibility. They have
no integrity manifest and `kubeproof verify` labels them `legacy, unsealed`.

## Reproducibility criteria

For two runs with identical chart, values, profile and execution options:

- Static checks, observations, findings and admission must match exactly.
- Runtime check assessments and finding severities are compared by check and
  resource. Differences are reported rather than normalized into a pass.
- Installation and Pod replacement durations are samples from separate runs;
  compare them with a tolerance of the larger of 5 seconds or 20% of the first
  run. Complete samples from comparable workload groups are used to compare
  sampled memory peaks within 25% of the larger peak.
- Sampled CPU is indicative. The CPU peak difference must not exceed the larger
  of 25% of the larger peak and an absolute allowance of the smaller of 20
  millicores and 1% of the profile's `max_sampled_cpu_per_pod`. If the profile
  has no CPU limit, numeric CPU repeatability is inconclusive. The check's
  execution status and assessment must still match exactly.
- DNS query sets are compared explicitly. An absent query is not evidence that
  the dependency disappeared; collection window and workload state matter.

These are review tolerances, not automatic readiness thresholds. They do not
make Kubernetes scheduling, image pulls, DNS activity or Metrics API sampling
deterministic. Tool and server versions should be checked before attributing a
runtime difference to the product.

`kubeproof compare FIRST_RUN SECOND_RUN` applies these criteria to two sealed
bundles after verifying both offline. It returns `0` for repeatable, `2` for
differences and `1` for an invalid or inconclusive comparison. A repeatability
difference is a quality signal about the experiment, not a new product finding.
The profile's actual CPU limit remains unchanged: any complete sample above it
is still evaluated by the existing runtime resource check.

## Milestone 2 repeatability exercise (2026-09-23)

Two static inspections of pinned cert-manager v1.21.2 produced identical input
identity, admission, checks, observations and findings. Both sealed bundles
passed offline verification. Sealed static bundles for pinned Argo CD and both
default and locally admissible kube-prometheus-stack configurations also passed
offline verification; the default stack remained `static_only` under the safety
policy.

Two consecutive runtime evaluations used chart SHA-256
`73a56e1728edd6c99f1f31082618c3259d279a76b7ebd3d4bdc5475c2442d34a`,
the same values and profile digests, a 30-second steady-state window and one
recovery target. Their evaluation IDs are
`7f5b3d7e-6833-47a6-8243-01592eeff7ee` and
`f029459e-4a6c-489f-9749-88200d73c574`. Both bundles passed `kubeproof
verify`; both clusters cleaned up successfully. Helm was `v3.21.1`, kind was
`v0.23.0`, and both Kubernetes servers reported `v1.30.0`.

| Comparison | Run 1 | Run 2 | Result |
|---|---:|---:|---|
| Static facts and checks | identical | identical | exact match |
| Runtime installation/resources/recovery/network | pass/pass/pass/inconclusive | pass/pass/pass/inconclusive | exact assessment match |
| Installation duration | 37.039 s | 32.533 s | within 5 s / 20% tolerance |
| Pod replacement readiness | 2.017 s | 2.023 s | within 5 s / 20% tolerance |
| Largest sampled Pod memory difference | 24.13 MB | 22.10 MB | within 25% tolerance |
| cert-manager controller peak sampled CPU | 0.01088 core | 0.00053 core | **outside 25% tolerance** |

The controller and cainjector CPU samples failed the *original* 25% relative
tolerance; webhook CPU and all comparable memory peaks passed. The Metrics API
samples every 15 seconds, and the two runs captured different startup moments.
This negative result is retained. It led to the bounded, policy-relative CPU
allowance above. Although this existing pair passes the revised comparison, it
does not count as independent validation of a criterion chosen after observing
its values. The revised criterion must be tested on new runs.

### Prospective check of the revised criterion

After fixing the comparison rule, two new cert-manager runtime bundles were
generated with the same pinned chart, values, profile, tool versions and
execution options. Their evaluation IDs are
`dfa7e50a-2768-45a5-9315-0f413ddf40c7` and
`478bd1b5-d06a-4b9d-8f08-2416aef8b523`. Both passed offline verification
and reported successful cluster cleanup. `kubeproof compare` returned
`repeatable`.

All static facts matched exactly. The four runtime assessments matched:
installation, resources and recovery were `pass`; network remained
`inconclusive` because no external DNS query was observed. Installation took
38.043 and 35.037 seconds; Pod replacement readiness took 2.019 seconds in
both runs. The same three cert-manager workload groups had complete Metrics API
samples. Their largest CPU peak difference was 0.011522 core (about 11.5
millicore), within the predeclared 0.020-core allowance for this profile;
sampled memory peaks were within 25%.

This validates repeatability of the recorded outcomes and the indicative
measurements for this workload and environment. It does not establish a
guaranteed CPU peak or prove the same behavior for other products.

### Second product: Argo CD

Two further runs used the pinned Argo CD 10.9.1 chart, the same strict profile,
a 30-second steady-state window and one recovery target. The evaluation IDs
are `8d5b578f-796a-4573-86f3-034bcb382160` and
`91ecebd2-cf8d-4fde-aa3e-4a97cb04a56a`. Both sealed bundles passed offline
verification and both clusters cleaned up successfully.

`kubeproof compare` returned `repeatable`: static facts and the four runtime
assessments matched; installation took 83.603 and 95.594 seconds, within the
20% tolerance; Pod replacement readiness took 2.023 and 2.034 seconds. Seven
workload groups had complete, comparable CPU/memory samples in both runs. The
largest CPU peak difference was about 9.64 millicore, below the 20-millicore
allowance for this profile, and all memory peaks were within 25%. The network
check was `inconclusive` in both runs; repeatability does not turn that outcome
into a `pass`.
