# Use browser control

Ricky can run profile-scoped Chromium sessions during an interactive terminal conversation. It
supports ephemeral sessions, dedicated persistent profiles, and explicit attachment to a local
Chromium debugging instance. Page control includes navigation, scrolling, tab selection, bounded
semantic snapshots, ordinary form entry, explicit consequential commits, dialogs, popups, and
local user handoff. It can also transfer reviewed files, use masked visual snapshots as a fallback,
and fill recognized protected controls from local protected-value aliases. Gateway-owned ad hoc
executions can use a separately enabled, guarded background surface; named jobs can use its
read-oriented subset. Attachment to selected tabs in your everyday browser is not supported.

## Install and enable Chromium

Install the Chromium build matched to Ricky's locked Playwright version:

```bash
ricky browser install
```

This explicit command stores browser binaries below `user_data_dir`. Normal startup and browser
tool calls never download a browser. Inspect readiness without launching Chromium:

```bash
ricky browser status
```

Then enable browser control in `<user_data_dir>/ricky.toml`:

```toml
[browser]
enabled = true
headless = false
```

Headed mode is the interactive default. Set `headless = true` when a visible window is not useful.

## Configure a persistent browser profile

Add a non-secret resource to the owning profile's `ricky.toml`:

```toml
# <user_data_dir>/profiles/personal/ricky.toml
[browser.resources.ricky-personal]
kind = "persistent"
description = "Ricky-owned personal browser profile"
headless = false
```

Set up authentication locally without constructing a model provider or taking a snapshot:

```bash
ricky browser setup personal/ricky-personal
```

Sign in and configure the headed Chromium window, then return to the terminal and press Enter.
Later chat sessions can ask Ricky to list browser resources and open `personal/ricky-personal`.
Ricky asks for fresh permission every time it opens a configured resource; that decision cannot be
remembered as a session grant.

Inspect readiness or delete one idle profile:

```bash
ricky browser resources --profile personal
ricky browser check personal/ricky-personal
ricky browser reset personal/ricky-personal
```

Reset permanently deletes that resource's cookies, local storage, cache, and other Chromium state
after confirmation. It leaves the resource configuration in place.

## Attach a dedicated local Chromium instance

CDP resources provide advanced, whole-browser attachment for a dedicated Chromium process. Start
Chromium separately with a loopback debugging port and a dedicated user-data directory, then add:

```toml
# <user_data_dir>/profiles/personal/ricky.toml
[browser.resources.local-debug]
kind = "cdp"
description = "Dedicated local Chromium debugging instance"
endpoint = "http://127.0.0.1:9222"
```

The endpoint must be an exact loopback HTTP address with an explicit port. It is resolved from
local configuration and is never accepted from the model or printed in normal resource output.
Use `ricky browser check personal/local-debug` before opening it in chat.

CDP exposes every eligible HTTP or HTTPS tab in that dedicated browser. Ricky disconnects without
closing the external browser, its contexts, or its tabs. It also leaves unsupported and overflow
tabs open and does not intercept later navigation in omitted tabs. Attachment, eligible-tab
discovery, and destination checks share one configured attachment deadline. Ricky cannot infer
whether an external CDP browser is visible, so local user handoff is unavailable for these sessions.
Do not use this mode with your unrestricted everyday browser profile. Ricky cannot
limit a CDP connection to a user-selected tab group.

## Understand ownership and cleanup

Each interactive Ricky runtime owns its browser connection. Ephemeral browser data lives below the
primary profile's private data directory and is removed when that runtime closes. A persistent
resource uses a dedicated Chromium directory below its owning Ricky profile and retains state after
close. Ricky never imports or opens an ordinary Chrome profile as a persistent resource. CDP
processes and tabs remain externally owned.

Configured resources use host-local exclusive leases. If another Ricky runtime has one open,
opening, checking, setting up, or resetting it fails as busy. Competing Chromium processes must not open the same profile directory. If Ricky cannot confirm that an owned Chromium process closed, it reports
the cleanup failure and keeps that resource busy rather than risking a second process on the same
profile.

Browser binaries are installation-owned and remain available across sessions. A gateway foreground
turn never owns or directly calls a browser. It can start, inspect, cancel, or reconcile a durable
execution whose worker owns one browser for that complete attempt. A named job can own one bounded
read-oriented browser. Browser tools remain unavailable inside workflows and resumed nonresident
sessions.

## Enable background browser control

Background ownership is disabled independently of interactive browser control. Start with the
smallest surface you need in `<user_data_dir>/ricky.toml`:

```toml
[browser]
enabled = true

[browser.background]
enabled = true
read_enabled = true
interaction_enabled = true
protected_values_enabled = false
commit_enabled = true
allow_ephemeral = true
allow_public_https_research = true
```

The switches are installation ceilings, not authority by themselves. The gateway route and
`agents.ad_hoc_background` policy must admit the browser capabilities, and every selected browser
capability receives an authenticated, immutable guardrail. Enable the corresponding delegated
authority evaluators for ordinary browser effects:

```toml
[authority]
enabled = true
allowed_principals = ["telegram:personal/bot:YOUR_TELEGRAM_USER_ID"]
max_effect_calls = 50

[authority.capabilities.browser_interact]
enabled = true
max_effect_calls = 50
allowed_profiles = ["personal"]

[authority.capabilities.browser_commit]
enabled = true
max_effect_calls = 3
max_financial_limit_minor = 25000
currency = "USD"
allowed_profiles = ["personal"]
```

Use the complete disabled-by-default example in `ricky.toml.example` for budgets and protected
value authority. Owner settings, the authenticated request, compiled contract, current claim,
browser resource revision, live destination, and durable budgets all intersect. No webpage or
model output can widen them.

An ad hoc gateway execution can research public HTTPS sites without naming a merchant in advance.
When it reaches a consequential action, it proposes the exact live origin and transaction for a
separate approval. Transaction dispatch is then constrained to that approved origin and occurrence.
Background resources must be Ricky-owned headless ephemeral sessions or configured headless
persistent profiles. CDP attachment and user handoff are unavailable in the background.

The guarded transaction surface supports semantic interactions, protected fills when the gateway
was locally started with the required vault unlocked, task-artifact uploads, downloads, and
semantic-first coordinate fallback. Gateway inbound messages are text-only. To
upload a file, first store it as an in-scope durable-task artifact and authorize its exact id in
the form `task/<profile>/<task-id>/<artifact-path>`. Compilation pins its size and SHA-256 digest;
the worker rereads it within the browser byte budgets and rejects it if it changed. Arbitrary host
paths, browser downloads, and message attachments are not background upload sources.

The worker closes its browser on completion, cancellation, failure, timeout, or lost authority.
Live sessions and prepared transactions are not restart-resumable. A lost parked browser
invalidates that approval; Ricky must navigate and prepare a new occurrence instead of replaying
the old one. See [Browser transaction approvals](browser-transactions.md) for the parked approval
flow and [Messaging and gateway](messaging-and-gateway.md) for commands.

## Interact with a page

Take a fresh semantic snapshot before asking Ricky to act on a page. Its element references belong
only to that session, page, navigation, and snapshot. Every dispatched action consumes the
snapshot and normally returns a fresh one for the next action. Ricky rejects invented, ambiguous,
changed, or reused references before dispatch when it can determine that safely.

Ordinary interactions include clicking a non-consequential control, replacing text, selecting one
visible option, setting a checkbox or radio, and sending a bounded navigation or editing key. They
are treated as external effects because Web applications can autosave or attach hidden handlers.
The CLI asks before dispatch by default and may offer an optional grant limited to that action kind,
the exact live browser session, and the top-level and target-frame origins.

Known submit, purchase, reservation, send, save, delete, and similar controls use the separate
commit operation. Every semantic or coordinate commit requires either a financial or generic
browser transaction envelope and a fresh action-specific approval. The commit cannot use a
remembered session grant or an allow rule to skip review; an ordered deny remains authoritative.
Denial and deterministic preflight rejection result in no browser dispatch. See
[Browser transaction approvals](browser-transactions.md).

Browser action results distinguish `performed`, `not_performed`, and `in_doubt`. `performed` means
the browser completed the requested action; it does not prove that a remote transaction succeeded.
Ricky never automatically replays an action whose effect may have become observable. Inspect the
page or confirmation evidence after an `in_doubt` result.

Dialogs are handled as part of the action that can create them. Ordinary interactions dismiss an
unexpected dialog. A commit can predeclare accept or dismiss, including prompt text. A single new
popup is registered and selected; ambiguous popup behavior is reported instead of guessed.

## Fill protected fields

Enable and initialize [Protected values](protected-values.md) before using a credential, payment,
or one-time-code alias. `browser_fill_protected` accepts one current target, one qualified alias,
and one safe field name. The model never supplies the raw value or destination. Ricky derives the
live protected-control category, top-level origin, and target-frame origin locally, applies both
ordinary browser policy and protected-value policy, and shows a value-free permission review.

One call fills one field and consumes its snapshot. Filling can trigger page JavaScript or
autosave, so it is an external effect and is never replayed after dispatch may have begun. It does
not click, submit, press Enter, or authorize a later transaction. Use a new snapshot and the
separate destructive commit review for a consequential control.

Ordinary fill, protected key entry, and coordinate clicks continue to reject recognized protected
controls. Plain HTTP protected use is denied even if ordinary browser destination policy permits
the page. CAPTCHA, passkey, SSO, and unsupported protected controls still require local handoff.

## Upload and download files

`browser_upload` accepts a current snapshot reference for an enabled file control. Before asking
for permission, Ricky reads the selected local files once and freezes their names, media types,
sizes, SHA-256 digests, and bytes. The review shows those facts without exposing a physical path.
If a source file changes while the prompt is open, Chromium still receives the reviewed bytes.
File selection can run page JavaScript, so it is an external effect, is never replayed
automatically, and does not offer a remembered permission grant. Ricky requires a fresh local
decision for every upload; an allow rule cannot suppress it.

In a gateway-owned background execution, `browser_upload` accepts only exact durable-task artifact
ids already compiled into that execution. Use `list_task_artifacts` to obtain the task id and
logical path; the reviewed guardrail uses
`task/<profile>/<task-id>/<artifact-path>`. Ricky pins the artifact digest and size before the run,
freezes the bytes before reserving the upload effect, and lists every artifact still associated
with the prepared transaction in its final approval. A changed, missing, out-of-scope, oversized,
or unapproved artifact stops before browser dispatch. Named jobs remain read-oriented and cannot
upload.

`browser_download` activates one current target in a Ricky-owned ephemeral or persistent session.
Ricky requires a fresh local decision for every download; an allow rule cannot suppress it.
The completed file must fit the configured byte ceiling before Ricky hashes and atomically
publishes it below
`<user_data_dir>/profiles/<owner>/downloads/browser/`. The model receives a logical reference,
not the physical path. Suggested filenames are reduced to plain filenames and an existing file is
never replaced. The logical reference can later be used as an attachment source while its owner
remains in scope. Durable downloads survive browser and chat shutdown.

Downloads are unavailable in CDP-attached sessions because Ricky does not own that browser's
download preferences or external storage. A server that omits or lies about `Content-Length` can
use more temporary space while Chromium receives the file, but Ricky still rejects an oversized
completed file before durable publication.

## Use visual fallback

Prefer `browser_snapshot` and its semantic references. When those do not represent a control,
`browser_visual_snapshot` captures only the current viewport, enumerates bounded DOM-derived
candidates, masks every editable, protected, and file control, and adds numbered overlays to the
PNG without changing the page DOM. Candidate facts and pixels are untrusted page input.

Screenshot disclosure is denied by default. Enable it in the browser resource owner's profile
configuration, not the installation configuration:

```toml
# <user_data_dir>/profiles/personal/ricky.toml
[browser]
screenshot_allowed_providers = ["openrouter"]
```

This allowlist is a standing profile-level disclosure decision. Once the pinned provider appears
in it, the read-only visual-snapshot operation does not ask for separate permission on each
capture. The policy names providers, not individual models. It does not change the chat's provider
or model and does not prove that the selected model accepts image input.

OpenRouter, Anthropic, and Claude Code can all encode Ricky's PNG input, but models routed through
those adapters can have different input capabilities. Before starting the chat, select a model
that its provider currently documents as accepting images:

```bash
ricky config model --profile personal
```

The provider and model are pinned when a chat starts. Use `/model` to inspect that selection. To
change it, exit and start a new chat with the new profile default or pass `--provider` and `--model`
for that invocation. Ricky does not automatically switch models when a visual snapshot is needed.

The current session provider must pass the resource owner's allowlist before capture and again
when its adapter materializes the bytes. A screenshot is stored as an opaque session-media
reference; paths, raw PNG bytes, and base64 never enter tool results or session history.

`browser_coordinate_click` and `browser_coordinate_commit` are last-resort coordinate operations.
Ricky first attempts to resolve an equivalent semantic target. If one exists, it directs the model
back to the semantic tool. Coordinate fallback is allowed only when the harness proves that no
supported semantic activation represents the intended target, the target is genuinely
position-sensitive, or a named semantic preflight stopped deterministically before dispatch. It is
never used after a semantic dispatch may have begun.

`browser_coordinate_commit` performs one reviewed click at an image coordinate. It is always a
destructive-risk transaction commit, requires the same financial or browser envelope as a semantic
commit, never offers a remembered grant, and cannot type, drag, or scroll. Before clicking, Ricky
maps the selected image point to a fractional CSS coordinate without truncation, then verifies the
exact screenshot generation, viewport, fractional scroll position, image scale, masked pixel
digest, bounds, nested hit-tested control, and any statically visible destination. File and
recognized protected controls are rejected. Any change to the recaptured masked viewport pixels
makes the target stale, so dynamic pages may require another visual snapshot. The coordinate commit
still receives its own fresh approval; an ordinary coordinate click remains inside its exact
interaction authority and cannot activate a target classified as consequential. The screenshot
provider allowlist does not approve browser effects.

Masked screenshots remain in private session storage until the resident runtime closes or `/clear`
starts a fresh session. At most the latest two retained images are projected into one model
request, subject to byte, pixel, token, and session-storage ceilings.

## Hand control to the user

For a CAPTCHA, passkey, SSO flow, unsupported protected field, or ambiguous interface, Ricky can
foreground a headed browser and end its turn with a fixed local instruction. Complete the step in
the browser, reply to Ricky, and have it take a new snapshot before continuing. Handoff is not
available in a headless session.

## Control destinations

Ricky permits HTTP and HTTPS top-level navigation. It rejects URL credentials and blocks loopback,
link-local, private, and other special network addresses by default. If a private destination is
required, configure its exact origin:

```toml
[browser]
allowed_private_origins = ["http://192.168.1.20:8080"]
```

The same destination policy applies to known link and form targets, redirects, and the final URL
of action-created pages and popups. Ricky closes a page that reaches a blocked destination and
reports the action as `in_doubt`. This is not a complete network sandbox: a public page can load
its own subresources, and sites may still detect or refuse browser automation.

Background navigation and every transaction destination require HTTPS. Exact private HTTPS origins
must also appear in the compiled execution ceiling; ordinary browser private-origin exceptions do
not silently grant a protected-value or transaction destination.

## Understand model disclosure

Ricky sends the configured model provider a bounded accessibility snapshot rather than raw HTML.
The snapshot can still contain personal data and untrusted instructions rendered by the page. URL
query values and fragments are omitted from model-facing metadata, and recognizable password,
credential, and payment control values are suppressed. These controls do not identify every form
of personal data. Use the protected-value consumer for recognized credential, payment, and OTP
controls. Its raw values stay local, but surrounding page content remains subject to the
configured provider's risk.

Ordinary agent-driven entry rejects recognizable password, OTP, payment, and credential controls.
Those fields require the dedicated protected-value consumer or local handoff. File controls
require the prepared upload operation. Ordinary values deliberately supplied to the model can
still appear in tool arguments and local permission previews.

Masks reduce accidental disclosure from common form controls; they are not complete redaction.
A screenshot can contain names, messages, account details, images, or other ambient personal data
anywhere else in the viewport. Allow screenshot disclosure only to a provider suitable for the
resource owner's data.

Persistent Chromium state is sensitive. It can contain cookies, local storage, cache, and account
sessions. Ricky confines it with owner-only filesystem permissions and does not serialize it into
tool results, but does not add application-level encryption at rest. Back up or expose the owning
profile directory only with the same care you would use for a signed-in browser profile.

Cookies, browser storage, arbitrary network bodies, and arbitrary JavaScript are not available
through browser tools. File transfer and screenshots use only the bounded operations described
above.
