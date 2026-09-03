# Browser control

This document defines Ricky's browser-control boundary. User-facing setup and behavior belong in
`docs/`; exact schemas and limits belong in code and tests unless they preserve a durable
cross-component invariant.

## Purpose and scope

Browser control gives Ricky a stateful Web client for semantic observation, foreground assistance,
and explicitly authorized background execution. Ricky owns the policy and model-facing surface;
Playwright is an implementation dependency behind a Ricky-owned backend boundary.

The subsystem does not promise universal site compatibility, CAPTCHA bypass, anti-bot evasion, or
perfect recognition of consequential controls. User handoff remains a supported terminal outcome.

## Ownership and dependencies

`ricky.browser` owns:

- browser resources, live sessions, pages, observations, targets, and actions;
- backend adaptation and browser-process lifecycle;
- per-page action serialization, timeouts, cancellation, and cleanup;
- browser-specific origin, navigation, observation, and file policy;
- conversion of browser state into bounded provider-safe tool results; and
- browser-specific effect evidence and ambiguous-outcome classification.

`ricky.tools` exposes narrow browser operations through the ordinary strict tool contract.
`ricky.runtime` constructs the browser service only after issuing a `ProfileScope`, contributes its
tools to the capability inventory, and closes the service through the runtime's owned-resource
stack. `ricky.config` owns all browser settings and paths. Provider adapters never receive
Playwright objects or browser-native wire formats.

Playwright's async Python API is the primary backend. `BrowserBackend` is a narrow in-process
protocol expressed in Ricky domain types; it is not a copy of Playwright's API and is not a public
plugin system. It must be sufficient for a Ricky-owned ephemeral browser first and later for
profile-owned persistent browsers, explicit CDP attachment, and selected-tab attachment.

## Execution surfaces and lifetime

A browser belongs to the runtime that performs the browser work:

- An interactive CLI session may own a browser for the life of its `SessionRuntime`, which spans
  the interactive chat.
- A gateway foreground turn does not launch or directly control a browser. It may prepare, start,
  cancel, or inspect a background execution under the existing execution contracts.
- An ad hoc background execution or named job owns its browser for the complete execution attempt,
  not for one model request, tool call, or workflow step. The runtime closes the browser on
  success, failure, cancellation, lost authority, or timeout. Named and scheduled jobs receive
  only the bounded read-oriented surface; protected use, mutations, and commits are available only
  to gateway-owned ad hoc executions in the current contract.
- A gateway-owned execution may park one exact prepared transaction while retaining its browser,
  resource lease, claim, and in-memory prepared effect. It performs no model or browser work while
  waiting. Denial or expiry closes normally; loss of the owner, browser, lease, or prepared effect
  invalidates the approval occurrence and cannot be resumed after restart.
- An attached browser process is user-owned. Ricky owns only its connection and must disconnect
  without closing the browser process or user-owned tabs.

The implementation does not add a browser daemon. A Ricky-owned browser process does not survive
its owning runtime. A configured persistent resource keeps only its dedicated Chromium profile
state between runtimes. An attached browser process remains externally owned and outlives Ricky's
connection. Durable run recovery belongs to unattended browser execution.

Each owner cancels and awaits browser tasks and closes pages, contexts, connections, the browser
process when owned, and the Playwright driver in reverse construction order. Cleanup failure is
reported without replacing stronger effect evidence from an interrupted action.
Background setup owns the run and browser-attempt lifecycle from the first durable write. A setup
failure or cancellation joins in-flight persistence and terminalizes every record it created before
propagating; it cannot leave a live run or active attempt without an owner.

## Profiles, resources, and storage

Browser resources use the canonical `ProfileResourceRef` identity. A runtime may select only a
resource whose owning profile is in its issued scope; model arguments cannot widen that scope.
Ordinary new browser state defaults to the primary profile.

Installation-owned browser binaries live below a configured `user_data_dir` subpath. Ricky never
downloads them implicitly during startup or tool dispatch. An explicit CLI installation operation
installs the approved Chromium build for the locked Playwright version.

Installation-owned browser mechanics and limits live in root configuration. A sparse resource
catalog may live in each owning profile's `ricky.toml`. Resource identities are always qualified as
`profile/name` before use. A persistent resource contains a description and headed/headless
preference; Ricky derives its directory and never accepts an arbitrary Chrome profile path. A CDP
resource contains an exact loopback HTTP endpoint with an explicit port. Endpoints and paths are
local configuration and never model arguments or provider-facing output.

Ephemeral browser data lives below the primary profile's generated browser directory and is
removed when its runtime closes. Persistent Chromium profiles live below their owning profile in a
directory derived from a digest of the qualified resource identity. They retain cookies, storage,
cache, and account sessions, use owner-only filesystem permissions on POSIX, and are sensitive but
not additionally encrypted by Ricky. Browser state never lives in project `.ricky/`. Tests use
distinct user and project data roots and prove that browser state touches only the intended root.

Ricky-owned ephemeral and persistent sessions may publish explicit downloads atomically below the
source profile's configured browser-download directory. A logical download reference contains its
opaque id, owner, sanitized filename, media type when known, byte count, and digest; model-facing
data never contains its physical path. Attempt temporary files are separately confined and removed
when the session closes. Durable downloads survive session cleanup. Attached CDP sessions cannot
download because Ricky does not own their download preferences or filesystem effects.

Session media artifacts use the generic `ricky.media` store below user data. Browser screenshots
have runtime retention: their private files and records are removed when the owning resident
runtime closes or the session is cleared. Canonical message history contains only immutable media
references. Browser screenshots and downloads never live in a Chromium profile or project
`.ricky/` tree.

Every configured resource requires one non-blocking host-local POSIX advisory lease held from
before launch or attach through confirmed cleanup. A failed or ambiguous close retains the lease
and surfaces cleanup failure; only confirmed process termination or connection teardown releases
it. The lock file contains only bounded safe owner metadata.
The kernel releases the lease after process death; an inert lock file does not keep a resource
busy. Non-POSIX configured-resource use fails closed until an approved portable contract exists.
Multiple agents may later share one profile only through one coordinator that owns and serializes a
single browser session, never by competing Chromium processes.

An attached browser records process and page ownership separately from connection ownership so
cleanup cannot infer that it may terminate an external process. Whole-browser CDP attachment is
limited to exact configured loopback endpoints and is intended for a dedicated debugging browser.
Ricky disconnects its Playwright connection but never closes external contexts, pages, profile
state, or the browser process. Unsupported or overflow external pages are omitted from Ricky's
control and left open. Ricky's context-wide navigation routing bypasses every omitted page. The
complete connection, page discovery, page-state, and destination-policy handshake is bounded by
the attachment timeout, including cancellable bounded hostname resolution. Initial attachment
fails if it cannot establish a bounded eligible page set; post-action overflow produces explicit
limited or uncertain evidence for every dropped page, including after earlier discovery
saturation.

CDP does not establish whether the external browser is visible. Attached sessions therefore
record visibility as unknown and cannot offer headed user handoff without a future explicit local
visibility signal.

## Canonical state and references

Serialized browser boundaries use strict, frozen, JSON-round-trip-safe Pydantic models. Live
Playwright handles remain private to the service and backend. The domain distinguishes:

- a profile-qualified configured browser resource;
- a live browser session with an opaque id, mode, lifecycle owner, and state;
- a page with an opaque id, session id, locally held actual URL, provider-safe URL projection,
  canonical origin, and navigation generation;
- a snapshot with an opaque id, page and navigation generation, bounded observation content, and
  truncation facts;
- an element target bound to exactly one snapshot and backend-issued reference; and
- an action result containing post-action page state and, when applicable, effect evidence.

Page ids and snapshot references are not selectors supplied by the model. The backend issues them.
A target is valid only for the session, page, navigation generation, and snapshot that produced it.
Navigation, page replacement, session closure, and a newer target-bearing snapshot invalidate old
targets. Every dispatched action also consumes its target-bearing snapshot, even if the action does
not navigate, and returns a fresh bounded snapshot when the resulting page is inspectable. Missing,
invented, ambiguous, or stale targets fail before dispatch.

The service serializes actions FIFO per page. Reads may run concurrently only when the backend can
prove they do not race an action or navigation. Browser actions are never automatically replayed
after dispatch may have become observable.

## Observation contract

Semantic observation is the default. The Playwright backend produces a bounded AI-oriented ARIA
snapshot. Ricky treats opaque session and page ids, canonical origin, navigation generation, and
truncation facts as trusted metadata. The provider-safe URL projection, page title, page-derived
text, accessible names, attributes, and instructions are untrusted external content.

Destination enforcement uses the exact URL locally. Provider-facing observations omit URL
fragments and query values by default while retaining the origin, path, and query-key names needed
to understand location. Locally known protected values and common credential-bearing parameters
are removed from every URL projection. Link targets remain locally actionable through opaque
snapshot references without requiring their complete URLs in model context. Values of password
controls and controls identifiable as credential or payment fields are suppressed. These bounded
rules do not claim to identify all ambient personal data rendered by a page.

Ordinary agent-driven fill and key entry rejects recognizable password, OTP, credential, payment,
and file controls. A separate protected-value consumer may fill one recognized protected field
from an opaque profile-qualified reference after local scope, field, destination, approval,
revision, and permission checks. File controls are exposed only through safe
snapshot metadata and the prepared upload operation; their values and selected local filenames
remain suppressed. A headed interactive session can instead return a fixed Ricky-authored user
handoff for CAPTCHA, passkey, SSO, protected-field, or ambiguous interface work. Handoff clears
current target references; the user completes the local step and the agent must request a new
snapshot.

Observation limits include depth, characters, pages, frames, and operation time. Truncation is
explicit. A model may request another bounded observation rather than receiving raw HTML, a whole
document dump, or silent truncation.

Provider-facing browser data always excludes cookies, browser storage, authorization headers,
request or response bodies, traces, arbitrary JavaScript results, filesystem paths, and unmasked
screenshots. Visual fallback captures only a bounded current viewport after the browser resource
owner's profile explicitly allows the pinned provider. It masks recognized protected, editable,
and file controls before producing PNG bytes, then composes deterministic numbered DOM-candidate
labels locally without modifying the page DOM. The source-owner/provider policy, admission
evidence, profile label, dimensions, size, and digest are checked again immediately before provider
encoding.

A visual snapshot binds the masked base-image digest, viewport and scroll metrics, coordinate
scale, navigation generation, and a bounded allowlisted candidate mapping to one ordinary browser
snapshot generation. It invalidates earlier target-bearing snapshots. Provider requests project no
more than their generic configured image count, byte, pixel, and token limits; older history keeps
references but receives fixed metadata-only omission markers.

## Tools, authority, and effects

Browser tools use three capability meanings:

- `builtin.browser.read` lists configured resource metadata and manages an ephemeral read-oriented
  session, navigation, page selection, scrolling, and semantic observation.
- `builtin.browser.interact` opens a configured authenticated or attached resource and performs
  ordinary page interaction that may be externally observable.
- `builtin.browser.commit` performs an explicitly consequential activation or submission with the
  strongest review and evidence requirements.

`browser_session_open` remains the unpermissioned ephemeral default. `browser_resources` lists only
safe metadata inside the issued profile scope. `browser_session_open_resource` accepts one exact
qualified identity, is mutating, requires a fresh local decision on every open, and offers no
remembered grant. Its preview states resource kind, ownership, visibility, and the possibility that
bounded authenticated page observations reach the configured model provider.
For a background execution, opening the exact contract-pinned persistent resource is runtime setup,
not a delegated external effect. The compiled resource identity, revision, authenticated-origin
ceiling, execution tool policy, and browser guard still constrain it; it does not reserve or consume
an external-effect grant action.

Only tools backed by the current background ownership, budget, evidence, and effect contracts
declare unattended use allowed. Named jobs may select the read-oriented subset only. A compiled
gateway-owned ad hoc transaction may additionally select reviewed semantic interactions, one
semantic-first coordinate-click fallback, bounded file operations, protected fill from a
gateway-resident unlocked broker, semantic commit, and one semantic-first coordinate-commit
fallback. CDP resources, headed handoff, prompt-each-use protected fields, workflow browser tools,
and arbitrary coordinate control remain unattended-forbidden.

Phase 4 adds four foreground-only operations. `browser_visual_snapshot` is a read-only local
artifact action under `builtin.browser.read`. `browser_upload` and `browser_download` are
external-effect interactions with fresh review and no remembered grant.
`browser_coordinate_commit` is a destructive external-effect click under
`builtin.browser.commit`, always reviewed once and never generalized to coordinate typing,
dragging, scrolling, or a remembered grant.

Phase 7 adds `browser_coordinate_click` to the guarded background interaction surface. It is one
external-effect click under `builtin.browser.interact`, not a selector, script, typing, dragging,
or scrolling API. It is available only after current semantic and masked visual observations and
harness-issued evidence prove that the intended nested hit target has no equivalent supported
semantic activation, or that a named semantic preparation ended deterministically before
dispatch. An equivalent target redirects the model to `browser_click`; dispatch uncertainty
prohibits fallback and replay.

Phase 5 adds `browser_fill_protected` as a foreground-only prepared external effect under
`builtin.protected_value.use`. The broker is constructed independently and injected into this
consumer. Model arguments contain only one current target, qualified protected-value reference,
and safe field name. The raw value crosses only an in-process protected-fill backend request and
never a serializable browser action, permission summary, result, event, or receipt.
Phase 7 additionally permits this same consumer inside a gateway-owned ad hoc execution only when
the gateway holds a process-resident unlock, the exact execution contract pins the resource,
revision, and field, and the background protected-value and browser guardrails both authorize the
live occurrence. Named and scheduled jobs remain unable to borrow that resident unlock.

Foreground interaction tools expose specialized click, fill, select, set-checked, and bounded-key
operations rather than one arbitrary action or selector surface. They use mutating risk and
external-effect receipts. A session grant may cover only one action kind on the exact top-level and
target-frame origins in one live browser session. The commit tool uses destructive risk and does
not offer a remembered session grant. For transaction commits, ordered denial remains
authoritative while an allow rule cannot replace the required fresh review.

Phase 6 requires one of exactly two typed envelopes on every semantic or coordinate commit. A
financial envelope covers any immediate or future payment, charge, transfer, withdrawal, deposit,
subscription, bid, paid reservation, or other monetary obligation. It identifies the exact proposed
amount and currency, payee, shown fees, timing or recurrence, and a safe protected-resource alias or
site-displayed funding-source label. Every other consequential action uses one generic browser
transaction envelope describing Ricky's intent, destination or recipient, material consequences,
disclosures, and expected browser-visible result. Individual form, reservation, application, and
message schemas are deliberately not introduced.

Envelope business details are model proposals derived from the conversation and untrusted page
content. The trusted review labels them accordingly and separately renders locally verified browser
binding facts. The model cannot mark business details as locally verified, and a completed browser
action does not prove settlement, delivery, or remote acceptance.

Both commit tools are prepared external effects. Preparation freezes the canonical envelope and
digest, action and dialog policy, current browser occurrence, target or visual coordinate, origins,
and any statically resolvable link or form destinations before review. A page-JavaScript-controlled
destination may remain explicitly unknown when the current top-level and target-frame origins are
exact and allowed. Exact destination URLs stay local; only safe projections reach model-facing or
persisted evidence. Dispatch revalidates all prepared mutable facts under the page action lock and
returns `not_performed` if any known binding changed.

The service dispatch boundary rejects semantic and coordinate commits that do not carry both the
prepared preflight and transaction evidence. Commit action results and resident latest-action
evidence require the transaction reference in performed, rejected, and ambiguous outcomes. A
prepared coordinate identity also binds the image and CSS coordinates, masked-image digest,
viewport, and private frame identity; changing any of them invalidates the reviewed effect.

Foreground transaction commits require a fresh interactive review for the exact prepared
occurrence. An ordered deny remains a ceiling, while an allow rule or session grant cannot bypass
the prompt. Approval is never remembered.

A gateway-owned background commit instead parks the live prepared occurrence and requires one
authenticated, source-bound, expiring durable approval containing the transaction id and one-time
code. The approval binds the execution, principal, conversation, review digest, envelope, live
browser occurrence, one-way session and page digests, resource identity and configuration revision
or ephemeral occurrence digest, pinned provider, complete browser budget ceiling, page generation,
origins, known destinations, target mode, protected-source evidence, and TTL.
The waiting execution revalidates every binding under the page action lock before it reserves and
dispatches the same prepared effect. Approval cannot be applied to a replacement browser or a
reconstructed cart. The envelope digest participates in stable logical effect identity and compact
action evidence, so changing any reviewed fact requires a new preparation and approval.

Dialog handling is fixed before the action that may create the dialog. Ordinary interaction
dismisses unexpected dialogs. A commit may explicitly predeclare accept or dismiss, including
model-supplied prompt text. Ricky does not suspend one action awaiting a later model tool call and
does not replay an initiating action to answer a dialog differently. Dialog messages remain
bounded untrusted page content.

The Web cannot provide a perfect edit/commit distinction. Text entry can autosave, controls can
have hidden JavaScript handlers, and a visually ordinary link can mutate server state. Therefore:

- interaction versus commit is an authority and review distinction, not proof that an interaction
  has no external effect;
- ordinary interaction is conservatively an external effect and receives no automatic replay;
- known submit, purchase, reservation, send, delete, or equivalent targets must use the commit
  surface, and the interaction surface rejects targets it can identify as consequential; and
- uncertainty after dispatch remains `in_doubt` until deterministic reconciliation or user review.

Effect identity binds the exact browser resource, session occurrence, page, navigation generation,
target or destination, action kind, and non-secret reviewed summary. Receipts state whether the
action was dispatched and what browser-visible postcondition was observed. A DOM change or model
claim alone does not prove the remote transaction succeeded.

The service may retain a bounded, resident, non-secret record of protected-resource aliases and
field categories successfully used on one page generation. It retains only the latest definitive
fill for each private frame-and-target occurrence; a later dispatched but ambiguous overwrite
removes the older evidence. A protected payment source is eligible only when every retained
payment-field occurrence names one alias. Preparation freezes that exact eligible source set and
dispatch revalidates it under the page lock. A financial envelope that names a protected funding
source must match that evidence. Navigation, teardown, and a dispatched or ambiguous commit clear
it. Website-stored or user-entered sources instead use a safe proposal label; the label may repeat
a masked identifier already displayed by the site but never derives a hint from protected
material. Strict envelope validation rejects recognizable unmasked card, account, IBAN, or card
security-code values in that label and rejects control characters in envelope display text.

Known local payment signals conservatively require the financial envelope, including current-page
payment protected-value use, recognizable payment controls, and recognizable purchase, donation,
transfer, subscription, bid, or paid-booking targets. Missing signals never prove that a commit is
non-financial. The generic review is visibly labeled non-financial so the user can reject a model
misclassification.

The live page retains bounded latest-action evidence. Cancellation after dispatch may have begun
invalidates the action snapshot and surfaces `in_doubt` on later page reads while the resident
runtime survives. Background execution additionally persists safe attempt, budget, logical effect,
envelope, approval, action, cleanup, ambiguity, and append-only reconciliation evidence in the
existing execution/job stores. It never persists raw URLs with query values, screenshots, DOM,
browser-native handles, physical paths, or prepared objects. A performed or in-doubt stable logical
transaction identity is a permanent no-replay boundary across later browser attempts.

One immutable `BrowserExecutionScope` is compiled before a background browser exists. It pins
execution mode, resources and revisions, permitted tool/operation classes, provider visual
disclosure, origin/destination ceilings, attachment and protected-resource scope, HTTPS transaction
requirements, and cumulative browser budgets. A runtime-local `BrowserExecutionGuard` reserves
budgets before work and rechecks live resource, session, page, origin, destination, target,
attachment, protected-source, envelope, current authority, and claim facts immediately before each
mutation. Page content and model output cannot populate or widen the scope.

A browser-commit delegation binds the current owner-configured financial amount and currency
ceiling into its durable grant. Other capabilities in the same execution cannot lower or erase that
commit-specific spend ceiling merely because they do not move money. The grant ceiling is not
transaction approval: every financial commit still proposes its exact amount and currency through
the separate live-occurrence approval before reservation and dispatch.

## Navigation and data-safety defaults

Default navigation permits `http` and `https`, plus backend-controlled blank pages. It rejects
embedded URL credentials, `file:`, `javascript:`, and other unrestricted schemes. Access to
loopback, link-local, and private-network destinations is disabled by default and requires an
explicit configured destination policy; deterministic test fixtures use a test-only allowance.
Redirects are evaluated against the same policy using the actual destination. Before dispatch,
the backend validates known effective link and form destinations locally. During dispatch, a
non-empty reviewed destination set also constrains the initial top-level navigation or popup
request, so a synchronous page handler cannot rewrite a reviewed link or form action to a
different destination before the request. An explicitly statically unknown JavaScript destination
continues through ordinary policy rather than gaining a fabricated binding. After dispatch, the
backend validates the final top-level URL of the initiating page and every popup; a page that
reaches a blocked or user-created blank destination is closed and the action remains `in_doubt`.

Uploads load and freeze exact attachment bytes, filenames, media types, sizes, and digests before
interactive review. Dispatch passes only that prepared in-memory payload to a current visible,
enabled, snapshot-bound file control and never reopens its source. A started selection is an
external effect and ambiguous failure is `in_doubt`.

A background upload has a narrower source boundary. Its reviewed guardrail names exact durable
task artifacts as `task/<profile>/<task-id>/<artifact-path>`. Contract compilation resolves each
artifact inside the issued profile scope and pins its digest and byte count. The worker accepts no
host path or other attachment class, freezes the current bytes within the execution budgets before
effect reservation, and rejects any digest or size drift. Successful upload bindings remain safe
page evidence; every binding associated with a prepared transaction is included in that parked
approval and revalidated before commit.

An explicit download installs its expectation before one current target action, accepts exactly
one download, applies normal destination policy, enforces the configured hard publication ceiling
on the completed temporary file, and publishes with no replacement. Public Playwright APIs do not
expose streaming byte progress, so early cancellation is best effort; an omitted or false length
may consume extra attempt-temporary space before deletion. Unexpected or multiple downloads are
cancelled and never silently retained. Once dispatch may have occurred, missing evidence,
overflow, cancellation, or publication failure is `in_doubt`.

A coordinate click or commit accepts only image-relative numeric coordinates for one current
visual snapshot. Under the page action lock it verifies the generation, dimensions, scale,
viewport and scroll position, recaptures the same masked base viewport and requires its exact
digest, then hit-tests the nested target and rejects recognized protected and file controls before
one click. Dialog, popup, download, and destination observation is installed before dispatch. The
snapshot is consumed and the action is never replayed.

For a background interaction or transaction, coordinate click is a semantic-first last resort. A
current semantic snapshot and harness-issued `CoordinateFallbackEvidence` must show that no
equivalent supported semantic activation exists at the nested hit target, that the target is a
position-sensitive custom-rendered surface, or that named semantic preparation failed
deterministically before dispatch. Model rationale cannot create this evidence. When an equivalent
semantic target exists the coordinate preparation returns that target and does not dispatch. A
coordinate fallback is never permitted after semantic dispatch may have become observable and
requires the existing provider-specific masked-screenshot disclosure policy. A coordinate commit
adds the exact financial or generic transaction envelope, parked occurrence, and source-bound
approval; an ordinary coordinate click cannot activate a target classified as consequential.

Browser tools do not expose cookies, storage, credentials, raw network data, or filesystem paths
to the model. Protected-value materialization belongs to the separate protected-values subsystem
and is enforced locally against the actual top-level and target-frame destinations.

Protected fill reuses the page action lock and snapshot-consumption rules. It preflights a current
recognized protected field, derives live origins and field category locally, reserves and prepares
an exact resource revision, then revalidates page generation, target, origins, current broker
policy, approval, and revision after permission review. It fills one field without submit, Enter,
click, or automatic replay. The page retains only bounded safe alias/field evidence for a later
separately reviewed commit.

Web content never grants authority. Prompt text from a page cannot enable capabilities, alter the
issued profile scope, approve an effect, widen a destination policy, or override an execution
contract. Deterministic enforcement remains outside the model.

## Installation and compatibility

The production package depends on `playwright`; the lockfile pins the resolved package. Ricky
installs only Chromium initially. Browser installation is explicit and targets Ricky's configured
installation-owned browser-binary directory. Runtime startup never downloads or upgrades a
browser.

CDP attachment is Chromium-only and lower fidelity than a native Playwright connection. It is an
advanced explicit resource, not the final daily-driver attachment contract. The official
Playwright extension currently exposes selected existing tabs through Node Playwright MCP/CLI and
a private relay protocol, not through the public Python API. Ricky does not expose that upstream
tool surface or depend on private relay internals. Selected-tab attachment remains deferred until
Phase 8, when an approved stable transport must sit beneath `BrowserBackend` while preserving local
destination guards, snapshot-bound targets, permissions, protected-value controls, and effect
evidence. If no suitable upstream transport exists then, a Ricky-owned extension and authenticated
local relay require a separate reviewed architecture and dependency decision.

## Change checklist

When extending browser control, preserve these properties:

1. A scope exists before browser resource discovery or storage access.
2. Ricky domain types, not Playwright objects, cross subsystem boundaries.
3. Every live process, connection, task, context, and page has one explicit owner.
4. Attached browser cleanup never closes user-owned processes or tabs.
5. Targets remain snapshot- and navigation-bound and actions are serialized per page.
6. No action is retried after it may have become observable.
7. Page content cannot grant authority or widen destination, profile, file, or disclosure policy.
8. Browser state and artifacts remain under their configured user-data and profile roots.
9. Every consequential commit carries one exact financial or generic browser envelope and receives
   fresh review or one exact source-bound background approval; no semantic, coordinate, policy, or
   grant path bypasses it.
