# KubeProof Milestone 3 — AI Evaluation Design

| Field | Value |
|---|---|
| Status | **Accepted design; first local CPU execution slice implemented, wider evaluation pending** |
| Date | 2026-09-25 |
| Prototype updated | 2026-09-26 |
| First scenario | Bounded CPU-load evaluation of the HTTP fixture |
| Framework decision | LangGraph orchestration with LangChain model/tool components |

## 1. Purpose and scope

The user describes an evaluation goal and company constraints. KubeProof turns
that description into a confirmed, typed request; an AI supervisor plans tests;
specialized agents execute admitted tasks; deterministic code controls execution
and assesses measurements; a report explains both findings and gaps. AI may
propose new test recipes by combining available typed tool capabilities. It may
not grant itself new permissions or change a completed observation.

The first **registered executable capability** is CPU load only. The plan
contract itself is heterogeneous: one supervisor-owned plan can contain tasks
from multiple registered capabilities, connected by dependencies. The prepared
HTTP fixture and local Docker calibration were starting material. A local kind
slice now installs the fixture Helm chart and sends real load to one selected
Pod, with Metrics API CPU samples and a sealed bundle. It does **not** measure
Service-level load balancing or prove a production SLO. Repeated in-cluster
calibration and model quality evaluation remain required before claiming AI
planning improves results. CNI/service-mesh compatibility, Helm-values optimization and HPA/VPA
recommendations remain future scenarios.

This design updates the *direction* for Milestone 3 in
[`architecture.md`](architecture.md): the earlier one-Reasoner/no-framework
default is superseded by the user-approved supervisor/worker topology and
LangGraph/LangChain choice. The historical ADRs in
`docs/adr/` are not current authority.

### Current prototype boundary

`kubeproof ai schema` prints the typed confirmed-request contract.
`kubeproof ai draft` uses a configured model to turn natural language into an
**unconfirmed** typed draft; missing fields remain missing. `kubeproof ai plan`
accepts an explicitly supplied confirmed-request JSON file, asks the model for
one plan and validates it without running any test. The supervisor's stable
instructions are capability-neutral; the catalog supplies the available typed
parameters for each run. The LangGraph planner/worker loop is exercised with
injected fake workers, including a test-only second capability to verify mixed
plans and independent parallel dispatch. Only `cpu_load` is registered in the
product catalog. A local UI now exposes a real fixture-Pod CPU worker with
static preflight, exact chart-digest approval, deterministic admission and
assessment, and a sealed evidence bundle. The default pilot runs one admitted
CPU trial directly, without a model or graph; AI mode uses the configured
supervisor and the same worker through the planning graph. This first workflow
has no persistent graph checkpointer, resume, general chart load contract or
multi-user authentication. The CLI `ai` commands remain non-executing.
See the [live CPU UI walkthrough](../experiments/live-cpu-ui.md).

The optional `ai` dependency group is required for these commands. The model
is selected at runtime with `KUBEPROOF_AI_PROVIDER` and
`KUBEPROOF_AI_MODEL`. For a local llama.cpp server, set the provider to
`llama_cpp`: only a loopback `/v1` endpoint is admitted, and the default is
`http://127.0.0.1:8080/v1`. LM Studio and other compatible endpoints use
`openai_compatible` with an explicit `KUBEPROOF_AI_BASE_URL`. Set
`KUBEPROOF_AI_API_KEY` when the endpoint requires authentication. Other API
providers use their LangChain integration package and standard credential
environment variable. No model name or provider is fixed in KubeProof.

### Local llama.cpp path

`llama.cpp` is an inference runtime, **not** the GGUF model itself. The operator
chooses the model file; KubeProof neither chooses nor downloads one. With a
locally installed `llama-server` and a selected chat-capable GGUF, start it on
loopback in a separate terminal:

```bash
llama-server -m /path/to/your-model.gguf --alias kubeproof-local --host 127.0.0.1 --port 8080
```

Then configure the client in the KubeProof terminal:

```bash
export KUBEPROOF_AI_PROVIDER=llama_cpp
export KUBEPROOF_AI_MODEL=kubeproof-local
export KUBEPROOF_AI_BASE_URL='http://127.0.0.1:8080/v1'
uv run --extra ai kubeproof ai doctor
uv run --extra ai kubeproof ai schema
```

`ai doctor` checks `/v1/health` and `/v1/models`, including the exact configured
model ID. It does **not** invoke the model, authenticate to a cluster or run a
trial. With a user-reviewed confirmed request, `ai plan confirmed-request.json`
is the next, non-executing inference check: it asks the model for typed JSON
and rejects invalid or out-of-policy plans. Passing `doctor` proves endpoint
availability, **not** planning quality. Structured-output reliability and
constraint adherence need several fixed synthetic cases, including ambiguous
requirements and rejected plans, before an operator should trust a selected
model in a live run. The UI permits AI mode only after explicit local approval;
it does not itself certify the model.
No real-model result is claimed until a specific served GGUF is tested and its
version and outputs are recorded.

For a remote API, choose its LangChain
provider integration and configure credentials through environment variables.
Non-loopback custom endpoints require HTTPS and an explicit key. The `draft`
and `plan` commands can send user input to that selected endpoint, so the
operator must decide where such data may go.

## 2. Confirmed responsibilities

| Component | Owns | Does not own |
|---|---|---|
| Requirements extractor (LLM) | Drafts typed fields from natural language and flags ambiguity/unmapped text | Finalizing constraints or inventing missing permissions |
| User | Confirms/corrects the structured request and approves critical actions | Implementing scheduler rules |
| Supervisor (LLM) | Creates and revises the versioned test plan; chooses test recipes and parameters; requests additional work from evidence | Starting tasks directly or overriding policy |
| Plan validator (deterministic) | Schemas, supported capabilities, dependencies, budgets, target and permission checks | Choosing investigation strategy |
| Coordinator (deterministic) | Readiness, dependency and resource-conflict checks, dispatch, retries and terminal conditions | Replanning the investigation |
| Specialized workers | Execute assigned tests through typed tools; publish own state, observations and follow-up proposals | Editing the plan or another worker's result |
| Tool/credential adapters (deterministic) | Scoped authentication and exact validated actions | Exposing secrets or a general shell to an LLM |
| Evidence/report layer | Immutable artifacts, grounded findings and explicit gaps | Turning narrative into unsupported facts |

Workers read the shared blackboard to understand context and task state. They
may write their own task status, observations, artifacts and suggestions. Only
the supervisor may change the plan, by issuing a new plan version. A worker's
suggestion is **not** a scheduled task until the supervisor incorporates it and
the deterministic validator admits it. Workers never call one another directly.

## 3. Input contract

The first request has five required categories:

1. authorized environment;
2. target workload;
3. success criterion;
4. resource constraints;
5. maximum experiment budget.

Natural language is an input convenience, not the executable policy. Extraction
produces a structured draft with source text for each filled field, plus
ambiguous and unmapped statements. A user reviews and confirms that draft before
planning. Missing required fields stop the workflow and request clarification;
the LLM does not silently invent values. The confirmed request is versioned and
frozen for a run.

The existing [company profile](profile.md) remains separate from execution
options: organization resource policy belongs to the profile; workload target,
environment and experiment budget belong to the evaluation request. The AI
intake may present them together, but it must persist their distinct meanings.
The local execution safety policy remains independent of both and cannot be
relaxed by either one.

The current confirmed-request and natural-language extraction schemas still
express a CPU objective. A future CNI/service-mesh objective needs its own
confirmed typed constraints and deterministic assessment; a generic plan
envelope does not make those goals executable by itself.

## 4. Plan, blackboard and execution contracts

The supervisor outputs one typed, versioned directed acyclic plan for the
overall objective. A task needs an ID, registered capability, parameters
validated against that capability's schema, dependencies, required/optional
role and rationale. The confirmed request supplies the target environment;
resource-conflict claims are computed by deterministic capability code, never
chosen by the model. The validator rejects
cycles, unknown capabilities, missing dependencies, out-of-budget work and
actions outside confirmed constraints. The coordinator computes which admitted
tasks are ready; an LLM never sets a task to `ready` by assertion alone.

Adding a real capability requires its parameter and observation schemas,
request-specific validation, deterministic resource claims and goal assessment
where applicable, an admitted worker adapter, and tests. Merely naming a new
capability in an LLM prompt cannot register or authorize it. The CPU capability
is the only product registration; the mixed-plan test uses a fake probe that is
never exposed to users.

Independent tasks may run concurrently only when their resource-conflict scopes
do not overlap. Two load tests against the same target, for example, are not
independent measurements merely because they have no graph dependency.
The current in-memory graph fans out disjoint ready tasks and combines their
records and tool-call counts. Durable atomic task claims/leases are still needed
before restart-safe external execution. A plan revision does not erase completed
tasks or their evidence.

The blackboard contains the confirmed request, current plan version, task
statuses, retry counters, approval status, observation/artifact references and
worker suggestions. Concurrent writes must be validated and attributed to the
actor/task that owns them. Raw observations and artifacts are append-only;
operational statuses may change through valid state transitions. The existing
sealed bundle remains the authoritative evidence output. A reusable test-recipe
catalog is separate from run state: saving a recipe does not grant it permission
to run on a different target or outside the next request's limits.

LangGraph coordinates the stateful workflow, checkpoints and approval pauses;
LangChain supplies model and typed-tool integrations. Product policy, the
ready-task algorithm, retry classification, credential handling, and evidence
validation remain KubeProof code, not prompts or framework defaults. A resumed
workflow must not repeat a non-idempotent cluster action: approval and action
are separate steps, with stable task IDs and deduplication at the execution
boundary.

## 5. Admission and approvals

Every proposed tool invocation is checked against the confirmed request,
independent safety policy and exact parameters immediately before execution.
Approval is tied to the specific action, target and parameters, not merely the
tool name. A changed plan or parameter set requires a new check. Critical
operations pause for explicit user approval; denial is recorded and does not
become a product failure. Authentication uses only scoped deterministic
adapters. LLM prompts and blackboard entries do not receive credentials.

There is no unrestricted shell or raw `kubectl` capability for an agent.
Novelty is allowed in **composition** of admitted typed operations, not in
bypassing their preconditions. Recipes are reusable templates, while each
execution still undergoes admission.

## 6. Bounded loop and outcome semantics

Two counters have different meanings:

- At most **five completed, valid trials with distinct parameters** may be
  used to search for a configuration meeting the goal. A completed trial that
  violates the goal is a valid negative result.
- At most **three execution attempts for one task** are allowed when the trial
  cannot be completed because of infrastructure/transport problems. These do
  not consume the five valid trials. After the third failure, that task is
  blocked, along with dependent tasks; independent tasks may finish.

Additional finite limits on plan revisions, wall time, tool calls and model
usage must be fixed in the run budget before execution. Their numeric defaults
are **not yet decided**. A supervisor cannot reset counters by changing plan
versions, duplicating a task, or saving/reloading a recipe. On budget exhaustion
the system stops and reports the reason; it does not claim success.

| Overall outcome | Meaning |
|---|---|
| `goal_met` | At least one valid configuration meets the confirmed goal and constraints with adequate evidence. |
| `not_met_within_budget` | Five valid distinct trials complete without finding a conforming configuration. This does **not** prove none exists. |
| `inconclusive` | A valid pilot misses the goal, or planning ends after valid negative trials before five distinct trials complete. |
| `blocked` | Required evidence cannot be collected, or execution stops without a valid trial. Independent results remain reportable, but an unsupported success claim is forbidden. |

Each individual check still follows the [truth contract](truth-contract.md):
execution status and assessment are separate. An endpoint that is unreachable
before load is an infrastructure problem; a service that fails because of the
applied load is a negative workload observation, provided the load and
measurement themselves were valid. The report names which case occurred and
links conclusions to evidence. An LLM may draft explanation but cannot add a
finding absent from validated state.

## 7. First vertical-slice validation

Implementation should progress from a deterministic baseline to AI planning:

1. Register and verify one bounded CPU-load capability against a load-balanced
   Kubernetes target, with trustworthy client and CPU measurements.
2. Freeze an example goal, constraints, workload definition and budget before
   comparing candidate parameters.
3. Test extraction and plan validation with synthetic LLM outputs, including
   missing constraints, unsafe parameters, cycles and invented capabilities.
4. Test dispatch with independent tasks, conflicting load tasks, retries,
   blocked dependencies, approval denial and process resume.
5. Compare AI plans against a fixed non-AI baseline on a small held-out set of
   CPU scenarios. Evaluate goal coverage, constraint adherence, unauthorized
   execution count, evidence traceability, run time and model cost; record
   negative results too.

No claim that the AI improves planning is accepted without that comparison.
The Docker-only [calibration note](../experiments/cpu-calibration-2026-09-25.md)
does not satisfy the Kubernetes end-to-end prerequisite.

## 8. Decisions still open beyond the first local slice

- Data-egress rules for remote model APIs and the minimum structured-output
  quality required of a selected local/API model. Provider/model selection is
  deliberately dynamic through environment variables, not fixed in the project.
- Calibrated default budgets and whether the fixed UI values should become
  user-editable. The current UI uses finite budgets and requires explicit
  approval for cluster creation, chart installation and load.
- Persistent checkpointer/catalog storage and recipe promotion policy.
- The calibrated Kubernetes workload target, latency threshold and error
  budget for the first controlled scenario.

## Framework references

- [LangChain overview](https://docs.langchain.com/oss/python/langchain/overview)
- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [LangGraph orchestrator-worker and parallelization patterns](https://docs.langchain.com/oss/python/langgraph/workflows-agents)
- [LangGraph persistence: checkpointer versus store](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph interrupts and idempotent side effects](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LM Studio OpenAI-compatible endpoints](https://lmstudio.ai/docs/developer/openai-compat)
- [llama.cpp server OpenAI-compatible endpoints](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
