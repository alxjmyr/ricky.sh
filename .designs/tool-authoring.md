# Tool authoring contract

A tool declares operational facts once. Each runner applies its own
authorization, persistence, and replay policy.

Every registered tool implements `ricky.tools.Tool` and declares these facts
beside its callable schema:

```python
class ExampleReadTool:
    name = "example_read"
    description = "Read one example."
    Params = ExampleParams
    risk = "read_only"
    capability_id = "builtin.example.read"
    effect_kind = "none"
    unattended = "allowed"
    review_mode = "policy"
    state_guard_id = None
```

Start from the canonical templates in [tool-templates.md](tool-templates.md),
then add one behavioral contract test with
`ricky.tools.testing.assert_tool_contract`. The helper dispatches the valid
case once, so use a fake client or an isolated store. It verifies strict extra
field rejection, JSON round-trip stability, optional provider-shape
normalization, deterministic external identity, safe validation feedback,
typed results, and receipt behavior.

```python
result = await assert_tool_contract(
    ExampleReadTool(fake_client),
    valid_args={"item_id": "item_1"},
    ctx=test_context,
)
assert result.content == "expected"
```

Every `Params` model in a new tool must set
`ConfigDict(extra="forbid", strict=True)`. Nested object models must forbid
extras as well; registry validation applies strict scalar semantics recursively.
`ToolRegistry` rejects invalid names, empty descriptions, non-Pydantic or
non-object parameter/result schemas, synchronous `run` methods, and invalid
contract versions. Capability registration additionally rejects incoherent
operational metadata before a tool reaches a provider.

The registry owns provider-variance handling. Before a tool or permission hook
sees arguments, it decodes JSON strings only for fields whose Pydantic
annotation unambiguously expects an object or array, then validates and dumps
one canonical argument object. Tool implementations must not add broad JSON
fallbacks, field aliases for provider mistakes, or semantic coercions. Keep the
declared `Params` model exact; validation errors return to the model for one
bounded repair sequence.

`capability_id` is the tool's one primary policy group. Built-ins use the
provider-neutral `builtin.*` namespace. Trusted external tools may use their
owner namespace, or set the field to `None` to request an owner-derived direct
capability. Capability specs provide shared descriptions and guardrails; they
never repeat tool names.

`unattended = "allowed"` states that the implementation supports unattended
execution. It grants no authority. Policy, immutable runtime contracts,
permissions, guardrails, budgets, and effect coordination can all narrow use.

`review_mode = "policy"` uses the ordinary ordered interactive permission
decision. A tool may instead declare `review_mode = "fresh"` when every
foreground occurrence requires a new trusted user response. For fresh review,
an ordered deny remains an absolute ceiling, but an allow rule or remembered
session grant is converted to `ask`; the runner offers no grant, and a missing
interactive responder denies. Fresh review is authority for only the exact
canonical call and prepared effect. It does not grant unattended eligibility.

Fresh-review external effects still prepare exactly once after static denial
and argument validation but before the prompt. Interfaces render the request
and return a response; they cannot downgrade the declaration or manufacture a
remembered grant. Do not special-case tool names in runners when this reusable
contract applies.

## Ricky-state mutations

Use `effect_kind = "ricky_state"` for mutations inside a Ricky-owned storage
boundary. The tool must enforce its subsystem transaction or concurrency
contract internally. If a runner must apply an additional wrapper, declare a
stable `state_guard_id`; startup validation requires that guard to be
registered.

## Deterministic runtime rejections

A tool or state guard may attach `ToolRuntimeFailure` to an error result only
when it has rejected the call before mutation or external dispatch. The typed
`state_conflict` classification carries a safe fingerprint of the observed
state and actionable recovery guidance. It must not classify transient failures,
post-mutation errors, or ambiguous effects. A receipt, when present, must say
`not_performed`. Neither the fingerprint nor guidance may contain secrets.

The agent loop exposes this evidence in `ToolCallFinishedEvent`. For the same
canonical arguments and unchanged conflict state, it supplies explicit recovery
context after two failing response rounds and terminates after three. Duplicate
calls within one response count as one round so the model has an opportunity to
repair. Observing changed state, success, or an unclassified outcome for those
arguments resets their conflict count; unrelated reads do not. The guard never
retries or replays a tool or infers completion. Old results and events without
classification retain their ordinary error behavior.

## External effects

Use `effect_kind = "external"` when the operation changes state outside
Ricky's transaction boundary. Complete deterministic local validation before
reservation, implement `effect_identity(args, ctx)`, and return an
`EffectReceipt` with `performed`, `not_performed`, or `in_doubt`. Once an
effect is reserved, missing outcome evidence is treated as `in_doubt` and is
not automatically replayed.

When identity depends on mutable input such as attachment bytes, also
implement `PreparedEffectProvider`. `prepare_effect` freezes the exact input
and identity before reservation; `run_prepared` dispatches that same payload
without reading the source again.

Foreground tool dispatch first normalizes and validates arguments and applies
static denial. For an otherwise allowed or reviewable prepared-effect tool it
then calls `prepare_effect` exactly once, uses the prepared safe summary for an
interactive prompt when supplied, and carries the object directly into
`run_prepared`. It never serializes the prepared object into session state or
events. Denial, invalid input, preparation failure, and cancellation do not
fall back to `run` or re-read the mutable source.

Unattended runners reconcile their terminal outcome against the effect ledger.
A model response cannot turn a rejected, `not_performed`, or `in_doubt`
external call into a successful run. Return the strongest receipt available;
include a stable provider reference when the external service supplies one.

An unattended prepared effect may require a capability-owned durable approval
after deterministic preparation but before reservation and dispatch. The live
owner keeps the exact prepared object in memory, persists only a bounded safe
binding and digest, and waits without model or tool activity. The matching
source-bound approval wakes that owner; it must revalidate current contract,
authority, policy, ownership, and mutable target facts before reserving the
stable logical effect and calling `run_prepared` once. Denial, expiry,
cancellation, or loss before reservation is `not_performed`. Owner or prepared
state cannot be reconstructed from the durable approval, and possible dispatch
remains `in_doubt` under the normal rule.

Declaring `unattended = "allowed"` for such a tool means its implementation can
participate in this harness-owned lifecycle. It does not let a named job,
workflow, generic runner, or model create an approval path. Runtime class,
compiled guardrail, authority evaluator, budget/effect ledger, and current
policy must independently select the exact operation.

```python
class ExampleSendTool:
    risk = "mutating"
    capability_id = "example.mutate"
    effect_kind = "external"
    unattended = "allowed"
    state_guard_id = None

    def effect_identity(self, args, ctx):
        return make_effect_identity(
            operation=self.name,
            target=args["recipient"],
            occurrence=args["request_id"],
            summary="Send the reviewed example message",
        )

    async def run(self, params, ctx):
        ...
        return ToolResult(
            content="sent",
            effect_receipt=EffectReceipt(
                disposition="performed",
                provider_reference=provider_id,
            ),
        )
```

Identities, summaries, receipts, events, and logs must contain no secrets.
Identity facts must be deterministic for one logical occurrence and must be
available without dispatching the effect.

`run_shell` is necessarily less precise than a purpose-built integration. Its
identity names the exact command, working directory, and occurrence, while its
receipt proves only the child-process outcome. It cannot enumerate or prove
the state of every host or external system contacted by that command.

## Protected-value consumers

A protected-value consumer receives the broker only through explicit runtime construction. The
broker is never placed in general `ToolContext`, and no tool may expose a generic raw-value getter
or arbitrary sink. Model arguments contain safe qualified references and purpose-specific facts;
the consumer supplies actual local destination facts and its own effect semantics.

When a protected value feeds an external effect, preparation binds the exact resource revision,
field, destination, target, occurrence, and in-process `SecretStr` material. Permission summaries
and effect identities use only the reference and safe metadata. Dispatch rechecks current policy
and mutable destination facts and uses that same prepared object. Denial and cancellation drop it;
post-dispatch interruption remains `in_doubt`.

Run `uv run ricky capability validate` after adding a tool. Before merging,
run the full project gate from `AGENTS.md`.
