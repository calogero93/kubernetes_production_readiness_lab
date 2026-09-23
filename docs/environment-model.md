# Composable Environment Model

| Field | Value |
|---|---|
| Status | Future product exploration; explicitly outside KubeProof Milestone 1 |
| Purpose | Reproduce an enterprise Kubernetes baseline and measure controlled variants |
| Last updated | 2026-09-21 |

## 1. Problem statement

Kubernetes products do not run against an abstract Kubernetes API. They run in a
specific platform assembled from network, DNS, storage, policy, service-mesh,
ingress and observability components. Configuration matters as much as product
identity.

The model must support questions such as:

- Does this product work with the Cilium configuration used by the company?
- Does enabling Istio ambient change readiness, connectivity or policy behavior?
- What latency and resource delta appears when ztunnel handles product traffic?
- Is a failure caused by the product, an environment component, or their
  interaction?
- Does upgrading one platform component change a previously accepted finding?

The goal is not to become a general cluster distribution builder. Environment
composition exists only to create controlled, attributable evaluation contexts.

## 2. Three models that must remain separate

| Model | Question answered | Nature |
|---|---|---|
| `CompanyProfile` | What does the company require or forbid? | Normative policy |
| `EnvironmentBlueprint` | What platform should kube-lab attempt to create? | Executable specification |
| `EnvironmentSnapshot` | What was actually installed and verified? | Immutable factual state |

Example: a profile can require a service mesh and default-deny networking. A
blueprint can request Cilium and Istio ambient. The snapshot may show that Cilium
installed but a required policy probe failed. The requirement remains unchanged;
the environment is not ready for experiments that depend on enforcement.

## 3. Environment composition

### 3.1 Blueprint shape

```text
EnvironmentBlueprint
├── identity and schema version
├── cluster: ClusterSpec
│   ├── provider and Kubernetes image/version
│   ├── topology and architecture
│   ├── container runtime
│   └── Pod/Service CIDRs and IP families
├── components: ordered dependency graph
│   ├── substrate: CNI, DNS, storage
│   ├── platform: mesh, policy, ingress, certificates, secrets
│   ├── observability: metrics, logs, traces, flow collectors
│   └── instrumentation: approved experiment support
├── safety controls
├── requested capabilities
└── resource/time budget
```

Some component slots are exclusive: a cluster normally has one primary CNI.
Others are repeatable: multiple metrics exporters may coexist. The registry owns
slot and compatibility rules; list order alone never resolves conflicts.

### 3.2 Illustrative Python DSL

```python
company_environment = EnvironmentBlueprint(
    cluster=KindCluster(
        kubernetes_version="<pinned>",
        workers=3,
        architecture="amd64",
    ),
    components=[
        Cilium(
            version="<pinned>",
            kube_proxy_replacement=False,
            network_policy=True,
            flow_observability=True,
        ),
        CoreDns(version="<cluster-pinned>"),
        MetricsServer(version="<pinned>"),
    ],
)

ambient_variant = company_environment.variant(
    name="istio-ambient",
    add=[
        IstioAmbient(
            version="<pinned>",
            ztunnel=True,
            waypoint=None,
        )
    ],
)
```

This illustrates the intended API, not implementation code. Versions are always
resolved to artifacts/digests before execution. A component constructor cannot
silently install an arbitrary chart.

## 4. Environment component contract

A registered `EnvironmentComponent` declares:

- stable type, implementation version and configuration schema;
- role and exclusive/repeatable slots;
- source artifact, version-resolution policy and accepted digests/signatures;
- installation phase and dependencies;
- known conflicts and versioned compatibility predicates;
- required cluster/kernel/runtime capabilities;
- claimed provided capabilities;
- typed install/upgrade/remove operations;
- readiness and positive/negative conformance probes;
- evidence collectors and expected artifact schemas;
- resource baseline collectors;
- cleanup, quarantine and failure semantics;
- trust classification and isolation requirements.

Components are installed through deterministic adapters. “Freely configurable”
means users can compose registered typed components and explicit configuration;
it does not grant agents arbitrary Helm or shell execution. A custom manifest can
be admitted as trusted operator input, but it provides no capabilities until a
registered probe verifies them.

## 5. Capabilities, not vendor conditionals

Scenarios depend primarily on verified capabilities:

```text
network.pod_connectivity
network.kubernetes_network_policy
network.default_deny_verified
network.flow_observation
network.dns_observation
mesh.workload_identity
mesh.l4_mtls
mesh.l4_authorization
mesh.l7_waypoint
observability.resource_metrics
```

Vendor-specific adapters translate component status into these capabilities. A
generic blocked-egress scenario asks for verified policy enforcement; it does not
contain `if cilium` branches. Vendor-specific scenarios remain possible when the
question concerns a vendor feature.

Capabilities have provenance: component, configuration, probe version, evidence
IDs and validation timestamp. Declared but unverified capabilities cannot satisfy
scenario prerequisites or security admission.

## 6. Lifecycle and attribution

Provisioning order is deterministic:

1. create the cluster substrate;
2. install bootstrap-critical components such as the selected CNI;
3. verify Pod, Service and DNS fundamentals;
4. install platform add-ons in dependency order;
5. run capability probes;
6. collect environment-only resource/behavior baseline;
7. seal the `EnvironmentSnapshot`;
8. admit and install the product;
9. run product scenarios;
10. collect final state, remove product, remove components and destroy cluster.

Failures before product installation are environment/infrastructure outcomes.
Failures that appear only through product/component interaction require evidence
from both subjects and are classified as compatibility findings. Component
DaemonSet CPU/memory is environment overhead; any product-induced increase is an
interaction delta and must be reported separately.

## 7. Variants and campaigns

An `EnvironmentVariant` is an explicit patch over a baseline:

- add/remove one component;
- change one component configuration;
- change one component version;
- change one cluster property.

The default experiment design changes one factor at a time. The resolved variant
is a complete immutable blueprint; reports do not rely on replaying a mutable
patch later.

A steady-state comparison campaign uses separate clusters:

```text
Campaign: impact of ambient L4 mesh on Product P

Run A
  Environment: kind + Cilium
  Product: P@digest
  Scenarios: S@versions

Run B
  Environment: kind + Cilium + Istio ambient/ztunnel
  Product: P@same-digest
  Scenarios: S@same-versions
```

The campaign comparison checks that controlled variables match. A mismatch makes
the metric/finding `not_comparable`; an LLM cannot waive it.

## 8. ztunnel impact example

Istio ambient uses a per-node ztunnel for L4 functions; waypoint proxies are a
separate optional L7 layer. Therefore “ztunnel impact” must not accidentally
include a waypoint unless the variant requests one.

### 8.1 Hypotheses

Representative hypotheses include:

- product installation and readiness are unchanged after workload enrollment;
- Cilium and Istio policy layers allow the intended flows and deny forbidden
  flows without split responsibility;
- mTLS enrollment changes source identity or connection behavior expected by the
  product;
- p95/p99 request latency, connection establishment and throughput remain within
  company thresholds;
- node-level ztunnel resource use and product-attributable deltas fit quotas;
- ztunnel restart or control-plane interruption does not require product restart;
- existing health probes, DNS, egress and telemetry remain useful;
- Cilium flow evidence loses or changes visibility when traffic is tunneled.

### 8.2 Controlled scenario set

Run the same workload and traffic profile in both environments:

1. environment health and baseline resource capture;
2. product install/readiness and functional smoke probes;
3. idle CPU/memory and connection inventory;
4. fixed warm-up and steady traffic window;
5. latency, throughput, errors, retransmits/resets and resource capture;
6. allowed/denied ingress, egress and service-to-service probes;
7. ztunnel restart and recovery measurement in the ambient variant;
8. product pod restart and connection recovery;
9. observability completeness comparison;
10. cleanup and leftover inspection.

### 8.3 Evidence and findings

Evidence includes environment snapshots, component health, Cilium flow records,
Istio/ztunnel metrics and logs, Kubernetes objects/events, active probes, workload
metrics and resource samples. Each measurement records warm-up, window, sample
count, traffic generator and clock.

An acceptable finding is specific:

> Under environment `company-cilium+ambient@digest`, enabling the ztunnel variant
> increased p99 latency from baseline A to B during workload W, while error rate
> remained C; evidence E1–E6. The result applies to the pinned component and
> product versions and does not include a waypoint proxy.

The report must not conclude “ztunnel is slow” from one workload or combine CNI,
ztunnel and waypoint effects into one number.

## 9. Cilium and Istio ambient interaction

The combination is not assumed valid because both components independently
report healthy. Their adapters must validate configuration compatibility and
joint probes.

Current upstream guidance includes Cilium/Istio chaining configuration and notes
that datapath and policy choices can affect traffic redirection and visibility.
Those exact rules are versioned provider knowledge, not timeless domain
invariants. At execution time kube-lab records which compatibility rule set and
upstream component versions were applied.

Joint probes cover at least:

- ordinary Pod/Service/DNS connectivity before and after enrollment;
- readiness/liveness/startup probe behavior;
- same-node and cross-node traffic;
- ambient-to-ambient and ambient-to-non-ambient traffic;
- standard Kubernetes NetworkPolicy positive and negative cases;
- identity/mTLS establishment and certificate rotation where testable;
- Cilium and Istio observability coverage for tunneled traffic;
- restart and node-drain behavior.

## 10. Transition experiments

Fresh-cluster A/B comparison measures steady-state effect. It does not answer
what happens when a company enables ambient mode around an already-running
product. That requires a separate deterministic transition scenario:

```text
install baseline environment
→ install and stabilize product
→ capture baseline under traffic
→ install/enable ambient component
→ enroll selected namespace/workloads
→ observe disruption, readiness and connection recovery
→ disable/rollback enrollment where supported
→ verify restored behavior and cleanup
```

The transition scenario records exact ordering, traffic continuity, connection
loss, rollout/restart requirements and rollback limitations. Its evidence cannot
be merged with steady-state campaign evidence without preserving phase labels.

## 11. CLI direction

Potential commands:

```text
kube-lab environment inspect examples.company:environment
kube-lab environment components list
kube-lab evaluate --chart <ref> --profile <profile> --environment <blueprint>
kube-lab campaign compare --baseline <blueprint> --variant <variant> --chart <ref>
```

The CLI displays the fully resolved component graph, digests, trust roles,
required privileges and expected capabilities before provisioning.

## 12. MVP boundary

Milestone 1 needs only the domain shape and one built-in minimal blueprint:
kind + kindnet + cluster DNS. It does not need a generic add-on marketplace or
campaign runner.

The first useful expansion is one pinned Cilium component with conformance
probes. The first composition proof should then add one pinned Istio ambient
variant and a small scenario set. A second CNI follows only after this demonstrates
that the component contract is not accidentally Cilium-specific.

Do not initially build:

- arbitrary environment charts supplied by agents;
- automatic Cartesian matrices;
- environment dependency solving beyond explicit registered rules;
- cloud-specific managed add-on provisioning;
- a universal benchmark score;
- automatic “optimal configuration” generation.

## 13. Acceptance criteria

The model is sufficient when it can:

1. resolve and fingerprint baseline and variant deterministically;
2. reject dependency/conflict errors before cluster mutation;
3. distinguish requested from verified capabilities;
4. classify environment failure separately from product failure;
5. attribute resource and network evidence to environment, product or
   interaction;
6. run a controlled baseline/ambient comparison without evidence mixing;
7. reproduce the resolved environment from pinned inputs;
8. explain why a result is or is not comparable.

## References

- [Istio ambient overview and ztunnel/waypoint responsibilities](https://istio.io/latest/docs/ambient/overview/)
- [Istio ambient traffic redirection](https://istio.io/latest/docs/ambient/architecture/traffic-redirection/)
- [Cilium integration guidance for Istio](https://docs.cilium.io/en/stable/network/servicemesh/istio/)
- [Istio platform prerequisites for Cilium](https://istio.io/latest/docs/ambient/install/platform-prerequisites/)

These are version-sensitive configuration sources. They inform registered
component compatibility rules but do not substitute for runtime capability
probes in a particular evaluation.
