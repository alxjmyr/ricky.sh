# Chrome-only browser transition

Status: implementation and repository verification complete; operator cutover and live acceptance pending.

The Chrome-only backend, ordinary setup, native navigation guards, and obsolete-code
removal are implemented. Browser tests pass on Chrome 153.0.8010.36 /
Playwright 1.62.0. Normal window closure and native credential-store selection
preserve a dummy manual-login HttpOnly cookie across headed/headless controlled
reuse; separate profiles remain isolated. The final full gate passed 2,486 tests
(including all 17 real-browser integration tests, no skips), Ruff, and Pyright
on 2026-09-11. Bundled documentation and the built wheel were verified.
Production has not been changed; the cutover scope question remains pending.
Live account trials are not established by local browser tests.

## Outcome and accepted scope

Ricky uses the host's installed Google Chrome Stable as its only supported
browser. A user signs into a dedicated Ricky browser profile and can later ask
Ricky to perform work through terminal chat or messaging, reusing those logins.
This refactor establishes repeatable authenticated browsing and preserves basic
local handoff. Autonomous browser resilience will be planned separately after
the Chrome cutover; it is not a completion gate for this plan.

The user approved Chrome-only support and starting browser profiles over. There
is one current user; preserving old Chromium profile contents or providing a
backward-compatible browser configuration is not required. Do not build a
profile converter, legacy backend, or compatibility shim. This plan does not
itself delete production data.

- Use installed Google Chrome Stable; no bundled Chromium, Chromium fallback,
  Chrome for Testing fallback, other browser brands, or Ricky-owned downloads.
- Keep Playwright as the private browser-control library unless evidence shows
  it cannot meet the required control contract. Its `chromium` API name is also
  used to control Chrome and is not a bundled-browser dependency to remove.
- Keep dedicated profile directories under the owning Ricky profile, resolved
  through `ricky.config`. Never open the user's everyday Chrome data directory.
- Chrome installation and updates belong to the host/operator. Ricky diagnoses
  missing Chrome and does not install system packages or update the browser.
- Preserve current Linux/POSIX support boundaries. Do not add operating-system
  support as part of this switch.
- Foreground/background task ownership and headed/headless browser visibility
  are separate concepts. Support both Chrome display modes; do not require a
  visible local desktop to run a genuinely headless task.
- No guarantee of universal CAPTCHA avoidance. Do not make stealth patches,
  CAPTCHA-solving vendors, proxies, or a hosted browser required dependencies.
- Existing approval, scope, protected-value, and effect-evidence guarantees
  remain requirements. This change does not grant standing purchase authority.

## Current owners and findings

Read `docs/development.md`, `.designs/architecture.md`, `.designs/browser.md`,
and `.designs/installation.md` before implementation. Read the profile,
tool-authoring, protected-value, and builtins contracts when changing their
boundaries. Update current contracts with implemented behavior, not history.

| Concern | Owners |
|---|---|
| Browser configuration and storage | `src/ricky/config.py` |
| Chrome discovery and readiness | `src/ricky/browser/chrome.py` |
| Launch, attachment, navigation | `src/ricky/browser/backend.py`, `playwright_backend.py` |
| Resource leases, setup, lifecycle, handoff | `src/ricky/browser/service.py`, `resources.py`, `setup.py`, `interfaces/cli/browser.py` |
| Runtime and background policy | `src/ricky/runtime/`, `src/ricky/executions/`, `src/ricky/browser/runtime_guard.py`, `guardrails.py` |
| Remote approvals and notifications | `src/ricky/gateway/`, `src/ricky/messaging/`, `src/ricky/notifications/` |
| Packaging and operator instructions | `pyproject.toml`, `uv.lock`, `.github/`, `docs/`, example configuration |

Setup now launches ordinary Chrome independently of Playwright. Controlled
browsing uses native Chrome navigation with CDP destination and response guards;
the former separate-client fetch-and-fulfill path has been removed. Service
workers remain blocked to preserve destination enforcement. Local tests establish
the new transport's behavior, not the cause of the reported live-site failures.

Background executions currently require owned headless browsers and forbid
CDP and user handoff. Exact transaction approval already retains a live worker
and browser while waiting for an authenticated Telegram response. Reuse its
ownership patterns, but do not treat verification handoff as purchase approval.

## Phase 1 — Establish the Chrome control and ownership design

- [x] Inventory all browser install/probe calls, binary-path environment
  plumbing, configuration fields, CLI references, test fixtures, packaging,
  release checks, and generated documentation references.
- [x] Define deterministic Chrome Stable discovery through typed settings and
  known supported host locations. If an executable override is necessary, keep
  it local and typed; reject missing or unsupported browser products. Browser
  version strings are diagnostics, not authentication of an external process.
- [x] Compare Chrome launched through Playwright's public Chrome channel with
  ordinary Chrome startup followed by public CDP attachment. Prefer the smallest
  public transport that meets behavior and ownership requirements. Do not
  couple Ricky to a private extension relay or upstream MCP tool surface.
- [x] Define one process/profile lease across setup, agent use, and handoff;
  distinguish owned Chrome processes from externally owned Chrome attachment.
  Specify cancellation, partial startup, disconnect, and confirmed termination.
- [x] Specify controlled versus manual operation: manual setup has no model,
  snapshots, automated input, or Ricky network interception. Agent control
  requires validated destinations and fresh observations before dispatch.
- [x] Resolve how a manually navigated session is re-admitted to agent control,
  including unsupported URLs, extra tabs, downloads, dialogs, and redirects.
- [x] Record the selected approach and revise the applicable current designs
  when implementing it. A daemon, custom extension, remote-access service, or
  new production dependency needs its own concrete architecture review; do not
  assume the Chrome-only decision selected those technologies.

Exit: a concrete launch/attach/lifecycle design and bounded comparison results.
Do not choose based solely on browser branding or a single CAPTCHA outcome.

## Phase 2 — Replace bundled-browser installation with Chrome readiness

- [x] Replace `install_chromium`, bundled-executable probing, and binary-directory
  assumptions with Chrome discovery and bounded readiness diagnostics.
- [x] Remove `ricky browser install`; retain useful `browser status` and
  resource checks with instructions to install Chrome externally when absent.
- [x] Remove `browser.binary_dir` and the Chromium-only selector; avoid a
  configurable browser-kind selector when only one product is supported.
  Update initialization templates and authored configuration examples.
- [x] Remove Ricky's `PLAYWRIGHT_BROWSERS_PATH` plumbing and download helpers.
  Preserve any still-needed subprocess cancellation and environment sanitation
  at their appropriate owner. Never dump child environments or raw diagnostics.
- [x] Ensure disabled browser support does not prevent ordinary Ricky startup
  on a machine without Chrome. Distinguish executable discovery from actual
  launch readiness, display availability, and automation-policy restrictions.
- [x] Keep Playwright in production dependencies and locked releases. Ensure
  sync, build, initialization, status, and ordinary startup download no browser.
- [x] Update installation contracts: Chrome updates independently of Ricky;
  status records useful versions and reports unsupported combinations clearly.

Exit: a clean Ricky installation uses preinstalled Chrome with no separate
browser download; missing Chrome produces an actionable, bounded failure.

## Phase 3 — Fresh profiles and ordinary manual setup

- [x] Launch `browser setup profile/resource` as ordinary headed Chrome using
  a fresh dedicated data directory, without Playwright attached or automation
  launch flags. Do not simply call the automated resource-opening method.
- [x] Retain profile scoping, private filesystem modes, exclusive resource
  leases, blank startup, and explicit lifecycle ownership. Setup must not
  inspect or expose authentication contents.
- [x] Define completion when Chrome remains open or has background processes;
  do not release the profile lease until owned-process closure is confirmed.
- [x] Handle terminal interruption, window closure, startup failure, missing
  display, and attempted concurrent use without corrupting profile state.
- [ ] Open the same fresh profile under agent control in headed and headless
  Chrome. Verify retained cookies/storage using local test credentials, and
  verify real login reuse manually without capturing secrets. Automated-to-automated
  and direct manual-to-controlled cookie persistence are verified. Real account
  reuse remains an operator trial.
- [x] Support ephemeral sessions with installed Chrome and retain cleanup of
  their temporary state. Persistent and ephemeral modes are not separate engines.

Exit: setup and automated use reuse the same dedicated Chrome profile safely;
the user's everyday Chrome profile remains untouched.

## Phase 4 — Preserve native browser behavior during agent control

- [ ] Run comparisons on the same host/network: ordinary Chrome, controlled
  Chrome without Ricky interception, and the complete Ricky backend. Record
  browser/library versions and safe outcomes, not cookies or authentication traces.
- [x] Replace navigation fetch-and-fulfill with native Chrome requests if the
  selected transport can enforce the complete destination contract. Cover
  redirects before disallowed requests escape, rewritten action destinations,
  popups, private origins, and unsolicited downloads. `route.continue_()` alone
  is not proof that redirect and response checks remain equivalent.
- [x] Audit service-worker blocking, routing-induced cache changes, viewport
  overrides, launch flags, and default browser features. Remove unnecessary
  differences only with evidence that required controls remain enforceable.
- [x] Keep reviewed uploads, download bounds, dialog handling, frames, semantic
  snapshots, masked visual fallback, and protected fills working on Chrome.
- [x] If native navigation cannot preserve a required guarantee, document the
  exact conflict and review the smallest contract change before implementation.
  Do not silently weaken controls or hide limitations behind a compatibility flag.
- [x] Verify external Chrome attachment, if retained, is explicitly scoped to a
  dedicated instance and that disconnect leaves external tabs/processes intact.

Exit: native-navigation behavior is validated with adversarial local fixtures.
Local transport comparisons are recorded in the browser design. Comparative live
site results remain pending; no CAPTCHA success rate is claimed.

## Phase 5 — Reuse Chrome from terminal and messaging executions

- [x] Route both surfaces through the same Chrome resource and backend owners.
  Serialize competing access; do not copy profiles between workers or let two
  processes open one profile concurrently.
- [x] Preserve headed terminal operation and owned headless background execution
  using installed Chrome. Keep execution ownership distinct from display mode,
  but defer new headed background admission, virtual displays, and remote
  takeover infrastructure to the later resilience work.
- [x] Keep browser ownership with the execution worker for the whole attempt.
  Prefer existing runtime lifetimes over introducing an always-running daemon.
- [x] Preserve transaction parking, source-bound approvals, stale-occurrence
  invalidation, and no replay after possible dispatch. Browser reuse never
  transfers a terminal permission grant into unattended authority.
- [x] Define explicit capability errors for unavailable display or unsupported
  attachment mode. Do not assume an external CDP browser is visible.
- [x] Test gateway/worker loss, timeout, cancellation, concurrent terminal use,
  and Chrome crash. Keep named jobs read-oriented and workflows browser-free;
  broadening those surfaces is outside this transition.

Exit: terminal and messaging can perform the same authorized browser task with
the configured Chrome profile and coherent lifecycle behavior.

## Phase 6 — Preserve basic local handoff mechanisms

- [x] Preserve the existing local handoff behavior on supported visible sessions.
  Stop agent actions during handoff and invalidate targets and prepared approvals
  before returning to fresh observations and normal authority checks.
- [x] Keep process ownership, attachment, disconnect, and profile leases explicit
  so future resilience work can build on them without competing browser owners.
- [x] Verify manual navigation or submission cannot cause stale actions or
  automatic replay when control returns. Do not infer that an external CDP
  connection supports local handoff without a validated visibility mechanism.
- [x] Document remaining verification failures and unsupported handoff modes as
  baseline evidence, rather than adding a new recovery subsystem in this refactor.

Exit: existing local handoff remains functional on Chrome, and lifecycle
mechanisms provide a sound foundation for later recovery improvements.

## Phase 7 — Cutover, documentation, and removal

- [ ] Stop active browser owners before production cutover. Inventory exact
  configured browser-state and binary roots through configuration helpers.
- [ ] Recreate dedicated browser profiles for Chrome; the user accepted losing
  existing Chromium cookies, sessions, cache, and storage. No migration or
  backward-compatibility layer is needed. Preserve resource names where useful.
- [ ] Remove obsolete browser configuration and old installation-owned Chromium
  binaries during explicit cutover. Check containment and ownership; leave
  unrelated Ricky state, downloads, protected values, and ordinary Chrome data
  untouched. Starting profiles over is not authorization to wipe the installation.
- [x] Update `docs/browser-control.md`, CLI reference, getting-started guidance,
  browser transaction guidance as needed, examples, current designs, and the
  project operating manual's real-browser terminology. Preserve unrelated edits.
- [x] Update test markers, fixture discovery, error messages, development
  instructions, and release/CI provisioning to require real Chrome. Provision
  Chrome at the environment layer, never through Ricky's runtime.
- [x] Remove dead installer/probe modules and obsolete tests. Keep meaningful
  lifecycle and isolation coverage; explain changed behavior assertions and
  obtain approval before replacing assertions required by the project rules.
- [x] Refresh bundled user docs through `scripts/bundle_docs.py`; do not edit
  generated builtin references directly. Verify the release wheel's bundled docs.

Exit: there is one documented Chrome-only path, no runtime bundled-browser
fallback, and no leftover download/install requirement.

## Phase 8 — Completion evidence

- [x] Unit and contract coverage: Chrome discovery, missing/wrong product,
  executable failure, independent browser updates, unavailable display, leases,
  profile roots, setup interruption, runtime cleanup, and Chrome-only attachment.
- [x] Real Chrome coverage: navigation/redirect interception, dialogs, frames,
  popups, persistent login state, downloads/uploads, semantic and masked visual
  observations, protected fills, approval drift, and process failure before and
  after possible effect dispatch. Test headed and headless paths explicitly.
- [x] Use distinct `user_data_dir` and `project_data_dir`; assert the intended
  root and that both unrelated project state and ordinary Chrome data are untouched.
- [x] Provision Chrome for the integration gate and confirm tests actually run;
  an all-skipped browser suite is not evidence of Chrome compatibility.
- [ ] Conduct a small cross-site acceptance trial on user-selected authenticated
  sites: login once, reuse after restart and on later days, execute from terminal
  and messaging, exercise existing local handoff, and report confirmed outcomes.
  Use non-consequential tasks first; any real purchase gets its normal approval.
- [ ] Record completed tasks, interventions, blocked tasks, and recovery results.
  A browser launch or successful click is not proof of task completion. Do not
  claim universal access from the sample or automate repeated live purchases.
- [x] Run the required commands below successfully and record browser versions
  and any skips.
- [ ] After operator cutover and live acceptance, preserve durable findings in
  designs/docs/tests and remove this completed plan according to the repository
  operating manual.

During browser implementation:

```bash
uv run pytest tests/test_browser_integration.py -v
```

For changed user documentation:

```bash
uv run python scripts/bundle_docs.py
uv run python scripts/bundle_docs.py --check
```

Before declaring repository changes complete:

```bash
uv run pytest && uv run ruff check . && uv run pyright
```

## Deferred — autonomous browser resilience

The user will return to this work after the Chrome refactor. Do not create or
implement a separate resilience plan yet. The eventual scope includes obstacle
recognition, autonomous semantic/visual recovery, bounded attempts and challenge
loop detection, verification of progress, and sanitized failure diagnostics.
Measure correct task completion without user intervention as the primary outcome;
human takeover is an exceptional recovery path.

That later work also owns automation detachment during verification experiments,
new manual-intervention execution states, remote takeover and resumption,
authenticated remote viewing, virtual displays, and any new headed background
admission needed for recovery. Preserve the same live session where possible;
restarts must invalidate prior occurrences rather than imply seamless resumption.
Chrome compatibility results should inform that design. Browser branding alone
is not evidence that automated sessions will rarely be rejected.

## Technical references

- [Playwright branded-browser support](https://playwright.dev/python/docs/browsers#google-chrome--microsoft-edge): installed Chrome is supported through the Chromium API; headless behavior and enterprise policies need validation.
- [Chrome remote-debugging changes](https://developer.chrome.com/blog/remote-debugging-port): Chrome 136 and later require a non-default user-data directory for remote-debugging switches.
- [Playwright navigation fetch implementation](https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/client/network.ts): `Route.fetch` uses the separate API request context.
- [Cloudflare supported environments](https://developers.cloudflare.com/cloudflare-challenges/reference/supported-browsers/): automated browsers are unsupported for production challenge solving; Chrome branding does not guarantee challenge success.
