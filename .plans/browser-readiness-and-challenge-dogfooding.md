# Browser readiness and challenge dogfooding

**Resume checkpoint:** read [browser-dogfooding-handoff.md](browser-dogfooding-handoff.md)
first for current state, preserved evidence, validation, and tomorrow's sequence.
The chronological notes below include superseded intermediate states.

Status: press-and-hold implementation and controlled acceptance are complete. The
broader readiness investigation and live Target acceptance remain open. Supported
verification attempts operate automatically in foreground and background execution,
with observations during a stationary hold, configured hard limits, owned release
on interruption, and post-release outcome verification. Defaults are 30 seconds per
hold, two attempts per page generation, and 60 reserved seconds per runtime.

## Hold implementation evidence — 2026-09-24

The implementation adds a separate `builtin.browser.verify` capability for foreground and
background use, exact visual candidate references, resident pointer ownership, independent
deadlines, attempt accounting, and joined cleanup. Ordinary transaction and protected-value
authority remain separate. The current contract is in `.designs/browser.md` and usage is in
`docs/browser-control.md`.

Live Target trials uncovered three concrete harness defects during development:

- Model-supplied coordinates mixed CSS and resized-image coordinates. The tool now accepts
  the exact visual candidate reference and resolves the target locally.
- Manual nested-frame hit testing stopped at the outer CAPTCHA container. Holds now validate
  the referenced locator and use native browser hover actionability before mouse-down.
- Stable-image comparison rejected the challenge's changing progress display. Visual captures
  during a hold are now masked observation-only images with no actionable candidate mapping;
  their tool text explicitly says missing candidates do not prove clearance.

The final live Target trial was session `session_d12bef808cc84caba8ccf02919d89b3d`.
The first hold reached the 30-second deadline; the second showed a checkmark and released
after approximately 14.19 seconds. A fresh post-release image still showed the verification
overlay and “Please try again.” This is a failed verification, not a successful pass.
The checkmark and subsequent rejection were independently inspected in the saved masked images.
Some normal visual snapshots also returned `visual browser snapshot failed`; a later semantic
snapshot timed out. The precise causes of those residual observation errors remain unresolved.
The browser and chat were closed after the trial; no sign-in, purchase, or cart action occurred.

Controlled real-Chrome tests pass for main-frame and iframe challenges, resized images,
animated feedback, release, and subsequent search availability. A complete named background
worker test also passes with ordinary interaction disabled and zero ordinary effect-call budget;
start and release retain durable effect receipts. Independent deadline, cancellation, observation
failure, and attempt-ceiling tests cover input ownership separately.
The service also rejects coordinate actions from observation-only captures that finish after
release; a deterministic delayed-capture regression covers that race.

Actual-model controlled acceptance passed in session
`session_224cbb3d099d46e98a0725f2009f53de` using OpenRouter
`openai/gpt-5.6-luna` and ephemeral headless Chrome. The local fixture put a non-submit
verification button in an iframe and displayed completion feedback after four seconds;
the duration was not supplied to the model. Fixture events recorded mouse-down at monotonic
26415.630 and successful mouse-up at 26428.950 (approximately 13.32 seconds held).
Ricky observed visual feedback, explicitly released, and obtained successful fresh semantic
and visual snapshots showing verification passed and Product search available. It then closed
the browser; the chat was also closed. This validates a similar interaction, not Target's
server-side acceptance decision. An earlier fixture version accidentally used an implicit
submit button and was correctly rejected as consequential; only the fixture was corrected.

Final validation: `uv run pytest -n 2 --capture=sys` passed all 2952 tests in 441.65 seconds,
including real Chrome and isolated release drills. `uv run ruff check .`, `uv run pyright`,
and `uv run python scripts/bundle_docs.py --check` passed. The separate browser lane also passed
all 41 tests. The final delayed-capture race was covered by all 45 service tests and the full run.

Earlier four-worker runs hit the previously observed two-second startup timeout in
`test_two_conversations_can_run_provider_calls_concurrently`. One two-worker run lost a worker
in the existing real manual Chrome setup test; that test passed in isolation and in the final
full run. Its exact native exit cause was not recovered. The final run retained native stderr
to expose a recurrence. No assertion, timeout, or test exclusion was changed to obtain a pass.
The isolated Xvfb display left by the crashed worker was closed. The default four-worker
startup timeout remains a validation limitation already present before this implementation.

## Resumed operator-observed hold trial — 2026-09-24

Session `session_7740cfb3a9e049cfbd6deb2fb5a89aad` used the same personal resource,
OpenRouter/Luna, and a visible browser. The operator confirmed resizing and a visible
verification challenge. Exactly one hold was requested, with no navigation, form entry,
sign-in, cart action, or automatic second attempt.

- Hold `browser_hold_170e34820b2d4d82834e123f5e44ced7` started on visual ref `d33`.
  Status at 9.3184 seconds was `holding`, with no stop reason and about 20.68 seconds left.
  Ricky explicitly released at 13.8158 seconds with stop reason `released`.
- The saved during-hold image shows a partially filled button, without a checkmark or
  rejection message. Ricky's subsequent audit identified no observed completion, rejection,
  during-hold observation failure, or deadline that justified releasing. The operator saw the
  hold but could not recall whether a checkmark appeared. This does not establish
  why Target rejected the attempt.
- A fresh post-release image shows the overlay and red “Please try again.” Clearance failed.
- Concurrent pre-hold and post-release visual/semantic calls each produced the exact visual
  error `browser screenshot disclosure binding changed`; separate visual retries succeeded.
  The during-hold visual call succeeded. Service disclosure revalidation rejects a changed
  cached visual binding; the concurrent snapshot sequence is a concrete race to isolate.
- Ricky initially described `d32`/`d33` visual labels as semantic evidence. Its audit corrected
  this: the available semantic excerpts did not independently verify the challenge controls.

The second and final permitted attempt in this runtime used sequential observations and explicit
instructions to keep observing while progress continued. Hold
`browser_hold_1c0f488583444063a27a755ff6623498` received four successful visual observations and
ended at the runtime deadline after 30.0455 seconds. Independently inspected images show partial
fill and later a three-dot processing indicator, not confirmed clearance. The fresh post-release
image was reported as showing “Please try again”; the following separate semantic snapshot did
not independently expose the challenge in its retained excerpt. No observation errors occurred.
The explicit release tool call after deadline was idempotent; it did not initiate the release.

Both physical attempts are now used. Sequential observations avoided the disclosure-binding
errors in this trial, and the second hold continued to its deadline rather than an unsupported
early release. Neither outcome establishes why Target rejected verification. Next useful control:
compare one manual verification in the same window/profile, without resetting the automation
attempt budget. Keep release timing, observation concurrency, and site acceptance separate.

The operator then reported a manual attempt taking roughly 5–10 seconds and also ending with
“Please try again.” Whether “clear” meant button completion or actual overlay disappearance
is being clarified. Manual rejection weakens a hold-timing-only explanation but does not identify
the cause. The same tab was navigated to Target once to test a manual-first attempt after refresh;
navigation succeeded without error (generation 2). Ricky was instructed not to interact with
the challenge after this refresh. No additional automated hold was started or budget reset.

The operator reported that the manual-first attempt after refresh also appeared to fail.
Refreshing alone therefore did not resolve the observed rejection. The next comparison keeps
the same persistent profile but uses ordinary manual Chrome through `ricky browser setup`,
without Playwright attachment, automation launch flags, or Ricky network interception.
The controlled browser session closed successfully before opening setup; the chat remains open.
No profile reset or cookie clearing was performed. This comparison changes the browser launch
mode as well as restarting the process, so a different result would not by itself identify a
specific site-side detection mechanism.

Operator requested network observation for the next instrumented verification trial. Capture a
bounded timeline of challenge-related request URLs/methods, status codes, failures, and relevant
redacted request/response fields; correlate it with pointer-down, visual progress/completion,
release, and final page state. Do not print or retain raw cookies, authorization headers,
verification tokens, or other secrets in diagnostic output. Distinguish server rejection,
transport/interception failure, and client-side state changes from actual evidence. Preserve the
current ordinary-Chrome manual comparison before starting a separately instrumented run.

## Live baseline findings — 2026-09-24

Installed Ricky 0.8.9 Python source matches checkout `018ddce` byte for byte.
The one commit after `v0.8.9` changes tests and development tooling, not runtime
source. Current Chrome is 154.0.8037.57 and Playwright is 1.62.0. The trial used
OpenRouter `openai/gpt-5.6-luna`, headed mode, and the existing persistent personal
resource. Historical Chrome version, initial authentication state, and exact
provider request/settings remain unverified.

Evidence below comes from the operator's visible-browser observations and pasted
terminal trace. Artifact details and exact errors were reported by Ricky after
inspection in the live chat; they were not independently recovered here.

| Condition | Observation | Verified outcome / failure owner |
| --- | --- | --- |
| Initial navigation | Homepage painted; Ricky reported it open without a separate snapshot. Operator saw a verification overlay appear about two seconds later. | Navigation succeeded; sustained task readiness was not established. |
| Readiness question while challenge visible | Semantic snapshot succeeded; Ricky explicitly reported no CAPTCHA and that the page was ready. Artifact inspection later reported `character_truncated: true`, depth 20, character limit 20000, and no challenge text in the retained result. | False readiness claim; model inference exceeded available evidence. Semantic omission versus truncation remains unresolved. |
| Visual inspection while blocked | Two visual calls failed; Ricky later quoted both errors as `visual browser snapshot timed out`. | Visual observation unavailable; internal timeout stage unresolved. |
| After manual verification | Fresh semantic snapshot found search controls; one visual call returned the same timeout. | Timeout persists after challenge clearance; not confined to the visible challenge state. |
| Search after manual recovery | Ricky filled “paper towels”, clicked a fresh target, and observed results. Operator independently confirmed matching search results. | Useful task completion in the same live session after one manual challenge intervention. No cart-add tool call occurred in the supplied trace. |

The visual timeout covers masked capture and candidate inspection under a shared
operation deadline; its text does not identify the stalled stage. Do not attribute
it to screenshot capture alone or increase the timeout without diagnosis.
No automated challenge interaction, challenge-request tool, or formal handoff was
tested in this trial. The original incident's challenge error remains unresolved.
Tool-call latency, token totals, complete provider projections, and image admission
were not captured. This is one baseline trial, not a three-trial condition result.

Next investigation: isolate the visual timeout stage and semantic truncation/frame
coverage with focused controlled diagnostics; then repeat the relevant live case.
Retain truthful readiness reporting as a separate regression requirement.

### Directly driven diagnostics

The operator authorized driving the existing chat through its tmux pane. A single
visual observation on the successful Target results page also timed out. Opening
a second session was refused by the configured one-session limit. At the
operator's request, Ricky then closed Target successfully and opened an ephemeral
headed session on Example Domain. Its visual observation completed the backend
stage but failed the service's pixel-limit check with
`browser viewport exceeds the configured screenshot pixel limit`. After the
operator shrank the window, visual capture succeeded and Ricky described the
Example Domain image. Navigating that same smaller ephemeral window to Target
produced a successful but truncated semantic snapshot and another visual timeout.
These are distinct failures; the pixel check occurs after
backend capture and cannot explain the earlier timeout by itself.

An isolated headless Chrome diagnostic exercised the unchanged `_PlaywrightPage`
visual method on synthetic pages at 1280 × 720 with a 10-second operation budget
and 100-candidate limit. A page with two viewport controls and one offscreen control
completed in 0.271 seconds. Otherwise equivalent pages with 1,000 and 4,000
offscreen controls both timed out at approximately 10 seconds; their first masked
captures completed in 0.055 and 0.063 seconds respectively. Neither reached the
second capture. The method inspects offscreen controls individually before
discarding them; the output candidate cap does not bound this scanning work.
This reproduces a control-inspection bottleneck independently of any website or
model, but does not yet identify Target's exact stalled stage. The diagnostic used
no personal browser profile and closed its browser afterward. No runtime code or
configuration changes have been made.

A subsequent fresh ephemeral headed Target trial used a temporary CLI wrapper
that measured the unchanged backend methods without emitting page contents.
The first masked capture completed in 0.331 seconds. Visual observation timed out
at 10.003 seconds during candidate inspection, after 65 descriptor calls started
and completed, before a second capture. This directly locates the timeout stage
for that live trial; it does not prove that every earlier timeout had the same
cause. The semantic snapshot also returned `semantic browser snapshot timed out`
in this trial; its internal stage remains uninstrumented. Temporary instrumentation
lives outside the repository; the original chat was closed normally before the
instrumented chat started. No runtime implementation or policy was modified.

Recommended first repair: avoid per-element protocol round trips for offscreen
visual candidates while retaining exact local target identity, masking, bounded
candidate output, cancellation, and before/after viewport validation. Verify with
synthetic offscreen-control coverage and repeat the live diagnostic. Treat
oversized-viewport handling and semantic completeness/readiness as separate work;
do not conflate successful image capture with reliable obstacle detection.

### Visual inspection repair

The operator approved implementing and validating the control-inspection repair.
The instrumented chat exited normally and closed its owned browser. Main-frame
visual scanning now filters offscreen element indices in a single in-browser
geometry pass before the existing detailed checks. Child-frame scanning retains
the existing coordinate path. Masking, candidate limits, target validation,
viewport/image comparison, and the operation deadline remain unchanged.

The same synthetic diagnostic now completes with 1,000 offscreen controls in
0.354 seconds and 4,000 in 0.594 seconds (single diagnostic runs, not a latency
guarantee), versus the previous 10-second timeouts. Regression coverage checks
three geometry protocol calls despite 1,500 offscreen controls, retention of a
later visible control and a partially clipped control, exclusion of a hidden
control, and candidate-limit truncation.

Validation: the focused browser integration file passed 21 tests; the complete
browser lane passed 39 tests. Two default four-worker full runs each passed 2,929
tests and failed only `test_two_conversations_can_run_provider_calls_concurrently`
at its two-second startup deadline. Its complete gateway file passed all 43 tests
in isolation. The supported two-worker full run passed all 2,930 tests in 415.82
seconds; Ruff and Pyright passed. No tests, deadlines, or assertions were weakened to obtain
that result. The four-worker gateway failure remains a validation limitation.
### First live retest after repair

A fresh uninstrumented chat used the same OpenRouter/Luna selection and headed
`personal/ricky-personal` resource. The operator resized the window and reported
a visible verification challenge, leaving it untouched. That observation was not
provided to Ricky before asking for sequential semantic and visual snapshots and
a readiness assessment.

Both snapshot calls succeeded. Ricky described the gray overlay, “Quick
verification” dialog, and press-and-hold instruction, and correctly reported the
page blocked for normal use. This verifies visual observation and independent
challenge detection in one live repaired trial, not automated challenge solving.

Ricky initially also attributed a “Press & Hold Human Challenge” control to the
semantic snapshot. After reading all three chunks of that retained semantic
artifact, it corrected the attribution: the 29,445-character artifact reported
`character_truncated: true`, depth 20, and character limit 20,000, with no
challenge text. The evidence came from the visual observation. This leaves both
semantic incompleteness and cross-observation attribution as unresolved findings;
successful visual capture does not fix either. The browser remains open with the
challenge untouched at this checkpoint. No challenge interaction was attempted.

## Objective

Use the reported Target.com session to determine where Ricky's browser harness,
model instructions, available actions, or provider behavior prevent useful work.
Improve bounded observation, accurate status reporting, and recovery before
deciding whether a different model is needed. Success means verified progress on
the user's task, not merely a successful navigation call or willingness to click.

This is a focused follow-up to [Chrome-only browser work](chrome-only-browser.md).
It does not replace that plan's outstanding operator acceptance or authorize
production cutover, profile resets, remote takeover, or new background execution
architecture.

## Incident and evidence

The user ran `ricky chat` through OpenRouter with `openai/gpt-5.6-luna`, using the
personal profile and a headed persistent `personal/ricky-personal` resource.
Session: `session_a972ef6889b84e409391bf9b80810ad0`.

1. The user asked Ricky to open Target.com. Resource opening and navigation
   succeeded; Ricky said the site was open without a separate snapshot call.
2. The user reported a verification challenge and asked whether loading had
   finished. Ricky requested semantic and visual snapshots. Both outputs were
   substantially offloaded, so the terminal transcript is not the full evidence
   presented to the model.
3. `browser_request_challenge` returned an error whose text was not shown.
   `browser_handoff` then succeeded with a request for the user to solve a CAPTCHA.
4. The user explicitly requested completion, including explaining an inability
   to press and hold. The model refused with a categorical security explanation.
5. Ricky closed its browser successfully when asked.

Confirmed in the inspected checkout:

- `src/ricky/browser/playwright_backend.py`, `navigate`: waits for
  `domcontentloaded`; this alone does not establish application readiness.
- `src/ricky/browser/tools.py`, `BrowserChallengeTool`: directs CAPTCHA requests
  to manual user action.
- `src/ricky/browser/playwright_backend.py`, coordinate preflight: rejects
  recognized CAPTCHA, passkey, and SSO targets with `handoff_required`.
- `src/ricky/browser/service.py`, `handoff`: supplies the exact initial CAPTCHA
  handoff sentence shown in the transcript.
- `.designs/browser.md` and `docs/browser-control.md` describe local handoff.

These findings establish a harness restriction, not that the coordinate guard
actually fired in this session. The later refusal explanation is model-generated.
The installed version, exact challenge error, complete provider request, actual
challenge implementation, and model behavior without these instructions remain
unverified. Do not infer them from output lengths or refusal wording.

## Phase 1 — Recover a reproducible baseline

- [ ] Compare the installed Ricky version/revision with the checkout. Record
  Chrome and Playwright versions, provider/model identifier, reasoning settings,
  headed mode, resource type, and whether the profile was already authenticated.
- [ ] Inspect the existing session through supported session/artifact interfaces.
  Recover the exact challenge arguments/error, navigation result, snapshot
  projections, image admission, tool descriptions, and active prompt/skills.
  Determine what the provider actually received, including offloaded evidence.
- [ ] Trace the challenge error to its owner: schema/target validity, stale refs,
  interface responder wiring, challenge binding, or another concrete cause.
  Do not assume the missing-responder branch was responsible.
- [ ] Reproduce the original two requests in a headed browser. Record observed
  outcomes even if Target does not present a challenge this time. Stop before
  purchases or other unrelated account mutations.

Keep raw authenticated observations in existing private runtime storage. Commit
only redacted findings and small synthetic fixtures; no cookies, credentials,
personal page content, or live profile copies. Avoid adding a new telemetry or
storage subsystem merely to perform this investigation.

## Phase 2 — Define and test task readiness

Read existing navigation, snapshot, actionability, timeout, and invalidation
behavior before proposing a new readiness API. Distinguish navigation completion,
visible rendering, actionable task controls, challenge blockage, and uncertainty.
There is no universal instant when a dynamic page is completely loaded.

- [ ] Evaluate existing bounded snapshots and targeted waits first. Candidate
  evidence includes lifecycle state, visible loading indicators or `aria-busy`,
  task-control presence/actionability, delayed dialogs/frames, redirects, and
  meaningful changes between observations. Treat these as evidence, not universal
  proof; page-authored signals remain untrusted.
- [ ] Require a relevant post-navigation observation before claiming the page is
  ready for work. If blocked, report the observed challenge; if time expires,
  report what remains unknown rather than claiming success.
- [ ] Use a bounded cumulative observation budget and cancellation. Avoid a fixed
  long sleep or a global network-idle requirement. Background traffic must not
  prevent useful work forever. Do not reload or repeat effects just to wait.
- [ ] Check the same behavior after SPA transitions and actions that trigger
  delayed rendering, with fresh targets after navigation or handoff.

| Controlled case | Required observation/outcome |
| --- | --- |
| Immediate usable page | Quick progress without unnecessary waiting |
| DOM ready before delayed task control | No premature readiness claim; continue once actionable |
| Delayed verification overlay or frame | Detect blocked state before proceeding underneath it |
| Continuous background requests | Progress when task controls are usable within budget |
| Loading indicator never clears or navigation stalls | Bounded uncertain/failed result; cancellation works |
| Redirect or SPA replaces controls | Observe the new state and reject stale targets |
| Challenge disappears after user action | Fresh snapshot and verified continuation in the same live session |

## Phase 3 — Review challenge behavior deliberately

- [ ] Separate ordinary consent controls, anti-bot challenges, OTP, passkey, SSO,
  and browser safety warnings. Determine the actual Target challenge from live
  evidence; do not classify every unusual button as a CAPTCHA.
- [x] Inventory semantic and coordinate restrictions, available gestures, tool
  instructions, approvals, and foreground/background differences. Establish
  whether a bounded press-and-hold is expressible at all before blaming a model.
- [ ] Recheck current provider guidance. The official OpenAI computer-use guide
  inspected on 2026-09-20 places CAPTCHA solving under action-time confirmation,
  not blanket mandatory takeover. This does not prove how Luna through OpenRouter
  behaves or establish another provider's rules.
- [x] Propose the precise supported behavior and consent boundary for confirmed
  user-requested challenge interaction. Keep verification consent separate from
  purchases, protected-value access, and later effects. Preserve a truthful
  fallback when policy, available tools, or site acceptance prevents completion.
- [x] Obtain maintainer approval before changing the current architectural
  contract, an existing public API, or regression assertions that encode the old
  intended policy. Present the concrete proposed changes and affected tests.
- [x] After approval, implement the smallest consistent change across policy,
  tool descriptions, actions, and documentation. If adding a held-input gesture,
  bound its duration and guarantee release on cancellation/error. Retain scope,
  target freshness, destination, permission, and effect-evidence checks.


## Phase 4 — Dogfood and isolate causes

Start with the original Luna setup. Change one factor at a time: observation
behavior, challenge instructions/policy, action support, then model. For model
comparisons, use the same tool surface, controlled fixture, task wording, and
relevant settings; record unavoidable differences. Separate controlled replay
from live Target trials, where cookies, challenge state, and site behavior vary.

Use a small predeclared trial count (initially three per selected condition),
not a large site/model matrix. Include one ordinary shopping navigation/search
task, a delayed-render fixture, and a challenge/recovery case. A live challenge
that does not recur is an inconclusive challenge trial, not a success. Compare
another model only after local restrictions and tool failures are accounted for;
select and verify that model in the future session.

Record each trial in a redacted table:

`condition | versions/model/settings | initial state | readiness evidence/time |
tool attempts/errors | refusal or approval | intervention | verified outcome |
tool calls/tokens/latency | failure owner`

Classify failure owner as model decision, harness instruction/policy, missing
gesture, tool/runtime error, observation loss, site rejection, or unresolved.
Score premature success claims, task completion, user interventions, and bounded
recovery separately. Willingness, successful dispatch, and site acceptance are
three different observations. After any completion attempt, inspect whether the
challenge cleared and the requested task can actually proceed.

## Implementation owners and validation

Read `docs/development.md`, `tests/README.md`, `.designs/architecture.md`, and
`.designs/browser.md`; read tool-authoring, protected-values, profiles, or builtins
contracts when changing those boundaries.

| Concern | Starting owners / coverage |
| --- | --- |
| Navigation, rendering, input, frames | `browser/playwright_backend.py`, `backend.py`; `test_browser_backend_contract.py`, `test_browser_integration.py` |
| Observation, target invalidation, handoff | `browser/service.py`, `types.py`; `test_browser_actions_service.py`, `test_browser_tools.py` |
| Challenge error and responder wiring | `browser/tools.py`, `runtime/composition.py`, CLI challenge owner; `test_cli_browser_challenges.py`, `test_browser_runtime_composition.py`, `test_browser_challenges.py` |
| Model-facing instructions and evidence | `agent/context.py`, browser tool descriptions, active skills; focused context/tool tests and controlled model trials |

Source paths above are relative to `src/ricky/`; test paths to `tests/`.
Use narrow unit/service tests for policy, parsing, transitions, budgets, and
errors. Keep only representative real Chrome fixtures for lifecycle/rendering,
frames, input ownership, and live snapshot guarantees. Synchronize fixtures with
events/state transitions and controlled clocks, not long sleeps. Cancel and
await owned tasks on failure. Update the coverage map if coverage moves.

During browser implementation, run `uv run pytest -m browser_integration` and
focused files with `uv run pytest tests/<file> -n 0 -v`. Before declaring repository
changes complete, run `uv run pytest && uv run ruff check . && uv run pyright`.
Update concise user docs and current design contracts for approved behavior;
refresh bundled user docs if those sources change.

## Completion criteria

### Active compatibility experiments (2026-09-24, continued)

Latest human baseline: ordinary Chrome setup (tool session 59228), same dev
profile and no debugging connection, also displayed a challenge. The user then
successfully completed it manually and agreed to leave this window open. This
is user-observed success; no automation is attached and no success screenshot
or network trace was captured. The setup process was subsequently polled live.
Leave this window untouched; do not refresh, attach, or close it merely to
collect more evidence. No new sign-in was reported, so do not infer restored
account authentication from successful verification.

Distinguish challenge incidence from challenge completion. Earlier controlled
holds and manual holds in controlled Chrome failed; this ordinary-Chrome manual
hold succeeded. The five launch-setting variants measured challenge incidence
only, not whether verification could be completed. They do not establish that
those variants cannot pass a challenge. Repeated attempts may have affected site
assessment, but no readable server decision establishes that explanation or any
cooldown duration. There is no evidence of a blanket inability to verify on this
profile/network. Further live comparisons should preserve explicit before/after
account and challenge state and avoid conflating a challenge appearing with an
unsolvable challenge.

Prepared next completion trial: `/tmp/ricky_indicator_trial.py` wraps the normal
Ricky CLI and redacted network recorder, asserting the explicit dev data root.
It adds only `--disable-blink-features=AutomationControlled` to owned-browser
launch arguments and verifies `navigator.webdriver` is false on the initial
blank page. A fake-launch check confirmed worker blocking, sandbox, and native
credential-store options remain unchanged; no live trial has run. This follows
the previously tested indicator-only challenge-incidence variant, but exercises
Ricky's actual owned-browser hold tools and current enforcement. Proposed trial
is one automatic hold with fresh visual feedback, existing hard deadline, no
retry, then fresh visual confirmation; challenge absence alone is not success
at solving a challenge. Successful ordinary setup window (session 59228) remains
open; the user must close it normally before its dev profile can be reused.
That setup process subsequently exited normally. Indicator trial launched as
Ricky chat `session_80cf6ca6656844b6a392c852d4d2ce16` (tool session 47137),
with `/tmp/ricky-network-20260925T013935Z.jsonl`. Launch verified
`navigator.webdriver=False`. Browser session
`browser_session_d79ccf73125a4294a058e5ff23a2ef6f`, page
`browser_page_dee775b7ce0e41249e6fc63d43e0b819`, navigated to Target.
First visual observation failed with `visual browser snapshot failed`; Ricky
stopped before attempting a hold. A separate diagnostic turn requested one
semantic snapshot followed by one visual snapshot, with no interaction.
Semantic observation succeeded and showed homepage metadata; visual observation
failed with `visual browser snapshot timed out`. No verification attempt was
made, so this is an observation failure, not a verification failure.
User visual confirmation is pending. A temporary timing launcher,
`/tmp/ricky_visual_timing_trial.py`, is prepared but not run; it wraps this same
indicator trial and records only stage durations, counts, and fixed error
categories for masked capture, screenshot, and control inspection. Raw exception
text, page content, and URLs are never printed. Stage durations overlap.

Timing trial subsequently ran after normal `/quit` of session 47137. Current
PTY 34352 runs `/tmp/ricky_visual_timing_trial.py`, chat
`session_6fa0d738226e43aab12469e00458575c`, with redacted network log
`/tmp/ricky-network-20260925T014559Z.jsonl`. Browser session
`browser_session_927e338b556947c1a3e53914283a72ac`, page
`browser_page_cd4f140cbbf041f3b4dcd5f3545df03b`; explicit dev root, same
indicator-only launch experiment. No hold or other page interaction occurred.
Two separately requested visual observations both timed out at 10.002 seconds:

| Observation | First masked capture | Screenshot within capture | Target descriptions entered | Description time (included in total) |
| --- | --- | --- | --- | --- |
| Initial | 2.306 s | 1.818 s | 57 | 3.630 s |
| Settled page | 0.378 s | 0.375 s | 73 | 4.471 s |

Both exhausted the operation deadline inside `_describe_target`; neither
reached the second screenshot/stability check. This isolates repeated control
inspection as the limiting stage for these two observations, rather than slow
screenshot capture alone. It does not explain the earlier generic visual
failure or establish the current visible challenge state. The existing main-frame
offscreen filter still leaves enough candidates to exhaust the budget through
serial visibility, geometry, facts, attributes, enabled, and editable calls.
Next repair investigation should reduce these round trips while preserving
Playwright control semantics, frame geometry, protected-control classification,
bounded candidate order, cancellation, and final image freshness checks. Do not
infer challenge clearance from the successful internal screenshot: neither tool
returned an image to the model. Chat and browser remain open and idle.

Candidate performance repair now inspects controls in batches of at most eight
concurrent read-only tasks, retaining DOM order and the existing Playwright
visibility, geometry, descriptor, masking, and image-freshness checks. Each batch
cancels and awaits unfinished tasks on failure or cancellation. The candidate
limit still bounds returned controls, with one extra match used to report
truncation. Focused snapshot tests passed (18), including new event-driven
ordering, bounded-concurrency, failure, and cancellation coverage. Real-browser
and type validation are running; live Target timing comparison remains pending.
The prior diagnostic chat (PTY 34352) exited normally to load the changed code
for that comparison. No production launch-default or service-worker change has
been made by this repair.

Live comparison with the candidate repair: PTY 79235, chat
`session_bd75cb3f0edd426eb9ba02db0acecf06`, log
`/tmp/ricky-network-20260925T015250Z.jsonl`, browser
`browser_session_c6b28af388c8447981b1d44643ac94af`, page
`browser_page_d46d063e0b8a4b97a41674fddeb1da4d`. First capture failed generically
after 8.853 s (5.046 s in screenshot; 41 descriptions entered; second capture
not reached). A separately requested settled capture succeeded in 3.476 s with
41 candidates and two screenshots totaling 0.322 s. Concurrent description
durations overlap and must not be summed as wall-clock time. Model correctly
identified the visible verification dialog. This supports the inspection fix
but leaves the initial-load generic failure unresolved.

One authorized automatic hold then ran on d41. Recorded mouse-down completed at
88.1626 s; mouse-up began at 97.2192 s and completed at 97.2524 s. Held observation
succeeded in 0.157 s, showed the default button with no confirmed progress, and
was followed immediately by the model's release request. Post-release visual
succeeded in 4.751 s and showed `Please try again`. Root independently confirmed
that text by viewing the saved image at original resolution (the default resized
view obscured the small text). Media identifiers:
`media_02b0ed086d6346d0b9b2a81178c7de39` (held),
`media_67f71879b90b49a3af2b022e1fcdd089` (post-release).
No retry occurred. On audit, Ricky acknowledged releasing before observing a
completion/failure signal. Thus this is a failed verification attempt with a
model-control confound, not a clean test of indicator masking alone. Collector
POST after release returned HTTP 200 with opaque response data; transport
success does not establish verification success. User observation is pending.

Validation: 18 focused tests, Ruff, and Pyright passed. The browser lane reported
41 passes plus a worker crash in the ordinary manual setup test; that exact test
passed in isolation (4.56 s). Full `uv run pytest -n 2 --capture=sys` is running
as PTY 10715. Do not claim full validation until its terminal result is read.
That full run subsequently terminated with exit code 143 around 48% progress,
without a complete pytest result. A rerun now retains verbose per-test progress
in `/tmp/ricky-inspection-full-tests.log` to locate any repeat termination. Full
validation remains unproven. The temporary timing probe also now instruments
`_visual_candidate` so a future launch can classify the initial-load failure
without printing raw exception content. The live chat remains on the earlier
probe version and is idle after its single failed hold.

Second/final authorized attempt in the same page generation used explicit
instructions to continue observing unchanged feedback. Fresh pre-hold visual
succeeded in 5.764 s (41 candidates) while full tests were running. Hold
`browser_hold_b0709ac6179142c298938d1f1f3f0325` started at network-log time
449.1993 s (mouse-down completed) and released automatically: mouse-up began
479.4763 s, completed 479.5118 s. Four held observations completed in
0.154/0.241/0.175/0.185 s. Ricky reported changing processing dots, no definite
completion/rejection, and `stop_reason: deadline`. Immediate post-release
capture failed in 1.818 s with a classified `frame_detached` screenshot error.
A separately requested follow-up visual timed out at 10.002 s during candidate
inspection (56 descriptions entered, only first screenshot completed in
0.396 s). Playwright also emitted an unhandled-future cancellation warning
waiting for the dynamic candidate selector's `nth(68)`. No subsequent input
occurred. Final site outcome is unknown: frame disappearance alone is not
proof of clearance. Both attempts in this generation are consumed.

The settled-page speed improvement is real but not sufficient for changing DOMs:
the current index-based locator scan can wait for vanished/reordered elements,
and masked capture can race frame detachment. These remain observation-reliability
issues. The full validation rerun remains live as PTY 23351 with verbose log
`/tmp/ricky-inspection-full-tests.log`; no complete result has been read yet.
Installed Playwright source confirms `Locator.element_handles()` uses
`Frame.query_selector_all()` and returns an empty list immediately when no
element matches, unlike `element_handle()` which waits. A possible next repair
is to pin each inspected candidate with the former, read all candidate facts
from that element, and dispose the temporary handle in owned cleanup. This
requires regression coverage for removal/reordering and careful preservation of
the retained action target mapping; it has not been implemented or validated.
Full validation rerun completed successfully: PTY 23351 exited 0;
`uv run pytest -n 2 --capture=sys -v` reported 2,955 passed in 445.61 seconds,
including real-browser and release lanes. Ruff and Pyright passed earlier on
this same code revision. This validates the bounded-concurrency repair, not
the broader browser-compatibility objective or Target verification success.

Later observation in the same live chat (no further automated input) succeeded
in 8.865 s with 74 candidates and two screenshots. Root viewed
`media_33f639ab901d406e9bf3a32861edadd8` at original resolution: Target homepage,
Account menu open offering sign-in, no verification overlay. Redacted network
log shows eleven redsky HTTP 200 responses between 479.52 and 510 s, first at
486.6441 s, about seven seconds after the second hold's release. This supports
delayed clearance rather than definite failure. Whether the operator intervened
during that interval is explicitly awaiting confirmation, so autonomous success
must remain qualified. No more holds were started.

Follow-up capture repair implemented: each candidate is resolved immediately
with `element_handles()`, its state is read from one pinned element, and handles
are disposed on success/error/cancellation. An immediate `evaluate_all` identity
check rejects changed index-to-element bindings as stale before retaining the
action locator. Masked capture retries once only when a captured frame detached;
it rereads viewport metrics and masks all current frames within the original
deadline. It never falls back to an unmasked image. Focused snapshot tests: 27
passed. Native offscreen geometry coverage was moved to ElementHandle without
changing its assertions. Initial browser lane: 40 passed, one test-wrapper
TypeError (ElementHandle.bounding_box has no timeout parameter); wrapper fixed,
exact failed test passed in 4.56 s. Ruff and Pyright pass on the latest revision.
Full validation is now running with log `/tmp/ricky-capture-full-tests.log`.
The live chat still uses the preceding code; this new repair has not yet had a
live Target trial. No launch-default or service-worker change was introduced.
Latest full-test process is PTY 22420. A search-continuation probe in the old
chat then stopped before any action: semantic observation reported unknown page
ID with no available pages; read-only `browser_pages` returned `browser session
has no open pages`. The chat is still live, but the formerly observed browser
page is no longer available. Closure cause has not been established. Do not
claim the window remains open or that search continuation was verified. No
new browser was opened during this probe.
The now-pageless chat subsequently exited normally via `/quit` (PTY 79235,
exit 0). No live diagnostic chat is currently owned. The temporary network
launcher now records page-close, page-crash, frame-detached, and context-close
events without content or URLs, for the next live run. This instrumentation was
not active during the unexplained closure and cannot establish its cause.
Latest capture-repair validation completed: PTY 22420 exited 0; full
`uv run pytest -n 2 --capture=sys -v` reported 2,964 passed in 422.07 s.
Ruff and Pyright also passed for this revision. Both browser and release lanes
are included in the full result. A fresh live diagnostic launch follows for
observation only, using the repaired code and extended lifecycle instrumentation.
New live session: PTY 76835, chat `session_82897fbebcac46928002270555ca1e94`,
network log `/tmp/ricky-network-20260925T022055Z.jsonl`, browser
`browser_session_2b4f17f5257d4fbb96ef9b92ce687715`, page
`browser_page_003b8cf0993c47ca8ac7a84757064fc8`. Same explicit dev root and
temporary webdriver-indicator change; launch confirmed false. Initial visual
succeeded in 6.789 s (88 inspected candidates, 74 described, two screenshots)
and showed the homepage without verification. The search-continuation probe
filled `paper towels` successfully, then clicked suggestion ref e1248 once;
click returned an uncertain timeout. No input retry occurred. One separate
read-only visual observation succeeded in 4.447 s (107 candidates inspected,
88 described) and showed the Quick verification dialog. The user independently
confirmed Target threw verification after Ricky tried to search. Therefore the
original browsing task remains blocked by renewed verification despite initial
homepage availability. This is not evidence of sustained clearance or completed
search. The repaired observation path successfully detected the challenge.
No hold has been attempted in this new session. Browser and chat remain open.
User then explicitly confirmed manual click-and-hold passed in this same
controlled window. Network log has HTTP 435 at 91.2681 s during search and
eleven later product-data HTTP 200 responses starting at 224.5846 s. The fresh
post-manual visual succeeded in 4.088 s. Root viewed original-resolution
`media_8a15c254a6a7482e8615b9ddab7bc98d`: homepage and Account sign-in popover,
no verification dialog, no search results. Ricky nevertheless said the image
showed the verification dialog remaining. This is a confirmed discrepancy
between the saved latest capture and Ricky's report. Investigate image delivery,
selection/order across turns, and model interpretation before attributing it
solely to model reasoning or running another hold. Manual success establishes
the current controlled environment is not an absolute verification blocker;
it does not prove the automatic interaction or launch configuration is reliable.
Without a new screenshot or tool call, explicitly naming the latest image made
Ricky correctly describe the Halloween banner and Account sign-in popover.
Static inspection shows newest-first bounded image selection and preserved
OpenRouter image order. This supports confusion between successive images but
does not retrospectively prove the earlier outbound payload. The generic tool
image attachment previously supplied one untrusted-content notice without
per-image provenance. Added an adjacent image ID, tool name, and call ID label
for each image, with guidance to use the latest relevant observation for current
state. Existing ordered-media coverage now also checks each adjacent label's
binding while retaining image-order and untrusted-content assertions. This is
a mitigation requiring live validation, not proof of eliminating model errors.
Full test log for this revision: `/tmp/ricky-image-label-full-tests.log`.
Synthetic provider comparison `/tmp/ricky_image_order_probe.py` generated two
local headless-Chrome screenshots (verification-required versus welcome/results)
without using a user profile or Target content. The same OpenRouter model
correctly reported the latest image in both orders, both with and without labels
(four requests). This did not reproduce the live failure or demonstrate a label
benefit. Treat labels as a clarity mitigation, not a validated cure. Focused
ordered-media test, Ruff, and Pyright passed. Full validation remains live as
PTY 22425; latest checked progress was 40% with no reported failure.
Attempted a post-manual search comparison using the explicitly labeled search
button instead of an unnamed suggestion. It stopped at the initial read: no
page IDs remained, so no fill or click occurred. New lifecycle instrumentation
recorded page-close events at 363.7302 and 380.9406 s, then context-close at
381.0014 s in `/tmp/ricky-network-20260925T022055Z.jsonl`. User confirmation
about manual window closure is pending. This is a confirmed context closure,
not merely an observation timeout; cause remains unknown. PTY 76835 is still
an idle chat with no usable browser context.
Image-label full validation (PTY 22425) terminated with exit 143 at 63%, during
ordinary job/workflow tests, without an assertion failure or final result.
This differs from the prior termination near 48%; no common failing test has
been established. A rerun uses a persistent terminal and retains output in
`/tmp/ricky-image-label-full-tests-retry.log`. Termination cause is unknown;
the earlier 2,964-pass full run predates the image-label change and is not a
completion gate for that new change.
User confirmed closing the current Target tabs/window after manual clearance.
The recorded page/context-close sequence is therefore explained by manual
cleanup, not a browser reliability failure. The idle chat (PTY 76835) received
`/quit`, but final polling showed exit 1 and a cleanup traceback, including
`RuntimeError: Event loop is closed` in subprocess-transport destruction. The
primary exception was not retained in the bounded terminal output; do not
attribute the error to core Ricky or the temporary observer without reproducing
it. No live diagnostic browser/chat is currently owned.
Image-label full validation retry is PTY 20348 and remains active.
End-of-session checkpoint supersedes that pending status: the retry reported
**2,964 passed in 449.14 seconds**, including real Chrome and release drills.
Ruff and Pyright passed for the latest image-label revision. See the linked
handoff for tomorrow's sequence and the private archive at
`~/.ricky/dogfood-evidence/2026-09-24/`. Source changes remain uncommitted.

Follow-up worker enforcement investigation: a persisted-profile regression also
failed with the current `service_workers="block"` default. Blocking registration
does not prevent already-installed workers from serving cached navigation.
The reproducible test is preserved in `browser-service-worker-regression.patch`
beside this plan; apply it to an isolated checkout and run
`uv run pytest tests/test_browser_integration.py -n 0 -v -k persisted_worker`.
It preinstalls workers in two local origins, reopens the profile using the real
backend, rejects the second origin, follows a cached redirect, and asserts that
the forbidden document's title-changing script did not execute. That assertion
failed with current code, confirming rejection occurs too late.

A candidate retained page CDP session with `Network.enable` and
`Network.setBypassServiceWorker` made that same-page case pass (enabling the
Network domain was necessary). The candidate passed 42 browser integration tests
and Pyright, but an additional local cached-popup probe still executed the
forbidden script. Pausing new page targets through `Target.setAutoAttach` and
setting bypass before `Runtime.runIfWaitingForDebugger` also did not prevent
the popup case. The incomplete runtime change was removed; the new regression
is an experiment patch, not a passing suite assertion. Existing tests were not
weakened. Candidate source remains at `/tmp/ricky-worker-backend-candidate.py`;
popup/target probes at `/tmp/ricky_sw_probe.py` and `/tmp/ricky_target_probe.py`.
No compatibility fix or complete worker-enforcement repair is established.
The current ordinary dev setup process (tool session 10557) was polled live;
the user subsequently reported opening Target with **no challenge at all**.
This repeats the ordinary-browser success after the controlled variants failed;
it strengthens the environment association without identifying its cause.
Next experiment: direct Chrome with an explicit loopback debugging port,
Target opened before any Playwright attachment, human observation first, then
attachment and bounded observation without navigation. This separates launch
with debugging available from attachment and automated navigation. The temporary
comparison script supports `native-cdp-delayed`; ordinary setup must close
normally before reusing its leased dev profile. No claim of challenge clearance
from this next experiment is yet established.

Delayed-attachment result: `/tmp/ricky-compare-20260925T012350Z-native-cdp-delayed`
loaded Target before attachment. The user reported a usable homepage. Attachment
occurred at 46.393 seconds; four observations through 61.561 seconds found no
challenge text, and the saved screenshot independently showed an unobstructed
homepage. Webdriver was false. This establishes usable loaded-page observation
after attachment in this trial, not success navigating while attached.
The screenshot's account label was generic "Account"; sign-in was not confirmed.
The user subsequently reported appearing signed out. Configuration verification
confirmed `~/.ricky` and the same persistent resource directory ending in
`3e66d784b31b3d7db9860829ef2b59da716efa3ee0c708c2066828e71485d79d`, with
only Chrome profile directory `Default` listed. Cause/time of account-state
change is unknown; no explicit cookie clearing or logout was performed.
The follow-up reload experiment (tool session 54174, artifact directory
`/tmp/ricky-compare-20260925T012634Z-native-cdp-reload`, loopback port 38783)
is waiting before attachment while the user checks the Account menu. No reload
has occurred. Temporary script cleanup now waits for Chrome to exit after
`Browser.close` before fallback termination; the previous immediate fallback
could race graceful shutdown, but no evidence links it to the account change.
The user confirmed Target is signed out and authorized continuing without
signing in again. Treat subsequent observations as signed-out trials, and avoid
attributing differences from the earlier signed-in runs solely to transport.
The staged reload run attached at 122.827 seconds; initial observation showed
no challenge text. Its within-session before/after reload comparison is ongoing.
Result: the page stayed clear in four observations through 137.995 seconds.
One controlled `page.reload()` began at 138.693 seconds; an HTTP 435 from
`redsky.target.com` followed at 141.689 seconds. Challenge text appeared by
149.769 seconds and remained at 159.827 seconds. The independently inspected
`after-reload.png` confirms a press-and-hold overlay. Webdriver remained false.
Some frames detached during page changes, producing bounded observation errors;
the final observations and screenshot succeeded. No hold was attempted.
This within-session result separates successful observation of a preloaded page
from challenged reload under attachment; it does not distinguish the reload
command from other attachment effects during a fresh page load. Next useful
control is a human refresh while this same Playwright connection stays active.
The user then manually refreshed with the connection active and reported the
challenge returned. The response observer also logged new HTTP 435 responses
around 217.497 seconds. Thus this trial does not isolate the Playwright reload
command as the cause; loading under an active attachment remains associated
with the challenge. The prior script could not disconnect without closing
Chrome, so a fresh `native-cdp-detach` trial was launched. It preloads Target,
attaches, observes, reloads once, observes, then disconnects Playwright while
leaving Chrome running for a human refresh. Pending artifact directory:
`/tmp/ricky-compare-20260925T013121Z-native-cdp-detach`; tool session 74667.
This trial already showed challenge text in its first observation at 17.600
seconds, immediately after attachment. No human pre-attachment observation was
collected for this repeat, so onset before versus after attachment is unknown.
The challenge persisted after one controlled reload. Playwright disconnected
at 52.863 seconds, leaving the same Chrome process open; manual-refresh outcome
after disconnection is pending. Do not infer that all fresh launches remain
clear from the earlier delayed-attachment trial.
The user refreshed after disconnection and reported that the challenge appeared
again. Disconnection alone did not restore the earlier ordinary-Chrome result.
This does not distinguish persistent process effects, remaining debugging
availability, profile state, or server state. The disconnected experiment was
closed through its owned cleanup; the same dev resource was reopened through
ordinary `ricky browser setup` for another no-debugging control. Cookies were
not explicitly cleared; current account state remains signed out.

User authorized experiments on automation indicators, ordinary Chrome launch
defaults, and allowing service workers. No production defaults have changed.
The temporary comparison driver is `/tmp/ricky_browser_compare.py`; it leases
the explicit dev profile, uses the installed Chrome, performs no challenge
interaction, records bounded frame text indicators and response status counts,
and saves a screenshot after approximately 20 seconds. All trials reuse one
profile sequentially, so cookie/server-state carryover prevents causal claims
from a single ordering. Screenshots are local and not committed.

| Trial / artifact directory below `/tmp` | Webdriver | Result |
| --- | --- | --- |
| `ricky-compare-20260925T010134Z-baseline` | true | Minimal Playwright, service workers allowed, no Ricky interception: challenge persisted; screenshot inspected; HTTP 435 observed |
| `ricky-compare-20260925T010220Z-webdriver-off` | false | Same plus `--disable-blink-features=AutomationControlled`: challenge persisted; screenshot inspected; HTTP 435 observed |
| `ricky-compare-20260925T010301Z-normal-flags` | true | Playwright defaults omitted, retaining profile and debugging pipe plus normal setup switches: challenge text persisted; HTTP 435 observed |
| `ricky-compare-20260925T010407Z-normal-flags-webdriver-off` | false | Combined ordinary flags and indicator change: challenge text persisted; HTTP 435 observed |
| `ricky-compare-20260925T010514Z-native-cdp` | false | Chrome launched directly with an explicit nonzero loopback debugging port, then public Playwright CDP attachment: challenge text persisted; HTTP 435 observed |

The normal-flags and native-CDP runs had one observed service worker (not yet
attributed to an origin); minimal default-flags runs had none. Thus allowing
workers alone is not evidence of worker activity on Target. The result does
not establish whether verification could be completed in these variants: no
holds were attempted. Reopened ordinary unconnected Chrome afterward for a
fresh human-observed control; result pending.

Local-only `/tmp/ricky_sw_probe.py` temporarily permits service workers with
Ricky's real backend, isolated temporary profiles, and two localhost servers.
Simple worker-generated redirects to a blocked network destination were
prevented; worker-generated attachment navigation returned `download_blocked`.
However, preinstalling a worker on the subsequently forbidden origin exposed a
real gap: a redirect served the forbidden page from its worker cache, its script
sent `/executed` to the forbidden fixture server, and only afterward did
`navigate()` return `destination_blocked`. The DOM and server counter both
confirmed execution. The final URL check is too late for this case. Worker
enablement requires a pre-execution enforcement design and regression coverage;
the current default remains blocked. Initial probe encountered a fixture
navigation race after a blocked redirect; reordered independent checks and the
final expanded probe completed successfully with owned resources closed.

Latest dev-profile comparison (2026-09-24): the shell's interactive `uv`
function selects `XDG_CONFIG_HOME=~/.config/ricky-dev` and
`RICKY_USER_DATA_DIR=~/.ricky`; earlier noninteractive launches missed that
function and used production data. Explicitly selecting the dev environment
resolved the root correctly. The user signed into Target through dev manual
browser setup without a challenge, then closed setup normally. The controlled
dev session `session_caad163eb42f417289d2b07e08cd4ac6` showed the challenge again,
confirmed by the user and saved screenshot. Semantic snapshot timed out.
The successful visual snapshot's candidate metadata contained account link
`d16`, "Hi, Alex , 5 new Target Circle bonuses", but that text was not visible
in the screenshot: the grey challenge overlay obscured the underlying page.
Treat this as underlying account-label evidence, not visible sign-in proof.
Candidate markers also appeared over obscured controls, an observation-quality
finding to investigate. Temporary network capture was partial: reading binary
post data as UTF-8 raised a recorder error. The temporary script now uses bytes
for subsequent launches. Binary handling and redaction checks passed, then the
corrected recorder ran in dev session
`session_fe610cd19e1a4ae5bc0c1ff0483cd93c` without request-body observation errors.
The user reported a failed manual attempt; the subsequent successful screenshot
showed "Please try again". No automated gesture was performed. The redacted
capture `/tmp/ricky-network-20260925T004128Z.jsonl` showed 19 initial
`redsky.target.com` HTTP 435 responses around 30–35 seconds after recorder start;
captured JSON shapes included `blockScript`, `jsClientSrc`, `appId`, and session
identifier fields (values redacted). Later collector POST bursts around 55–58
and 87–90 seconds received HTTP 200, with JSON shapes containing `do: null` and
redacted string `ob`. Request bodies were recorded only as opaque byte lengths.
HTTP 200 does not establish verification success. Manual press/release times
were not instrumented, so do not label individual bursts as distinct attempts
or derive hold duration. Some response bodies were unavailable (12 generic
Playwright errors and 9 timeouts during initial loading). Evidence supports
server-supplied blocking during page load, but does not identify the failed
verification's decision reason or prove a particular browser-detection cause.
The subsequent ordinary-Chrome comparison used the same dev resource without
clearing cookies. The user reported that the challenge briefly flashed and then
disappeared without interaction; Target showed the account signed in. Thus the
manual hold failure is associated with the controlled browser environment in
these trials, not exclusively with automated input. This sequential comparison
does not isolate startup flags, service-worker policy, interception, timing, or
server/profile state as the cause. Ordinary setup remains open for inspection.

- [ ] The original challenge error is explained with evidence, or explicitly
  remains unreproduced with the missing evidence identified.
- [ ] Controlled readiness cases pass without unbounded waits or premature
  success claims; interruption preserves resource and input ownership.
- [x] Challenge behavior matches an explicitly reviewed contract and communicates
  actual limitations instead of inventing a universal provider prohibition.
- [x] Redacted dogfood results distinguish verified progress, human recovery,
  unresolved site limitations, and model-specific outcomes. No universal site
  compatibility claim is made from a few trials.
- [x] Required checks pass for implemented changes. Durable findings belong in
  designs, docs, code, and tests; remove this active plan once its work is complete.
