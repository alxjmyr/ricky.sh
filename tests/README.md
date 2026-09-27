# Test ownership and local execution

See [the development guide](../docs/development.md#run-tests-locally) for local
commands. `uv run pytest` includes every lane. The core lane excludes only real
Chrome and released-installation drills; it retains real stores, transactions,
leases, recovery, provider adapters with fake transports, and tool policies.
The browser boundary file has one `xdist_group` to avoid running several large
DOMs and process-failure drills simultaneously. Other tests use small scheduling
batches, including the independent release recovery scenarios.

Chat send-it coverage stays in process: `test_chat_permissions.py` checks responder
isolation and workflow approval provenance; `test_fresh_review_permissions.py` keeps
the normal and automatic tool-review paths under the same preparation and denial
contract. `test_chat_send_it_integration.py` covers chat composition, browser commit
revalidation, and protected-value destination policy using the real service owners.
CLI command, prompt, and per-turn context coverage lives in the existing chat and
CLI test files. The real-browser and unattended suites retain their boundary coverage.

## Keep expensive coverage at its boundary

Use compact HTML for gateway journeys. The large checkout DOM belongs to
`test_real_chrome_modal_amount_preparation_and_final_transaction`, which explicitly
requests 180 background sections. That test still checks modal scoping, numeric
editing, financial preparation, bounded tool output, and exactly one submission.
It starts with the modal open; the gateway checkout separately checks the opening
click, amount editing, and commit through three performed effect receipts.
The other real-browser tests retain navigation interception, redaction, process
loss, persistent profiles, and CDP ownership coverage.
The navigation/snapshot journey also checks `navigator.webdriver` in real Chrome
to guard the owned-launch automation-indicator default without another browser fixture.

`test_real_chrome_visual_scan_skips_offscreen_controls` owns the large offscreen
visual-candidate fixture. It checks bounded per-element geometry calls, retained
visible and partially clipped controls, and candidate-limit reporting. Existing
visual-mask and coordinate-freshness coverage owns frame geometry, protected
controls, and stale-image rejection.
`test_playwright_backend_snapshot.py` checks bounded concurrent visual inspection,
DOM-ordered results, candidate truncation, and joined cleanup on cancellation or
inspection failure without launching Chrome.
It also covers pinned-element disposal, reordered-locator rejection, and a
single fully masked recapture when a frame detaches. Real-browser mask and
freshness tests exercise the native element-handle observation path.

`test_browser_holds.py` owns independent deadlines and joined release cancellation.
The hold cases in `test_browser_actions_service.py` cover attempt ceilings, rejected controls,
observation failure, release while observation is blocked, and rejection of coordinates from
hold observations that finish after release. `test_browser_unattended.py`
checks separate verification budgets with common effect receipts; `test_browser_guardrails.py`
checks capability scope and legacy budget serialization. `test_browser_hold_job.py` runs a
named read-oriented worker through start, observation, release, cleanup, and durable accounting
with no ordinary mutation budget.
Post-release worker coverage also checks that background briefing permits the
separately exposed verification tools and that a released hold is followed by a
new visual observation. `test_browser_tools.py` checks distinct holding,
released, and uncertain-release guidance without treating input receipts as
verification verdicts. The real Chrome hold journey owns
native input, resized visual references, iframe targeting, animated observations while held,
feedback-driven release, and verified continuation. Agent-loop tests check cleanup before
turn completion and after interrupted model work.

These focused tests replace repeated full-checkout scenarios:

| Guarantee | Coverage owner |
|---|---|
| Duplicate replies cannot consume a challenge twice | `test_execution_challenge_replies.py` checks delivered receipts, wrong senders, duplicates, expiry, and restart; `test_browser_challenges.py` also races two replies |
| User waiting can exceed the active-work budget without resetting cumulative wait capacity | `test_browser_challenge_wait.py` advances only the budget owner's clock, coordinates live tasks with events, and retains a real-timer expiry test |
| Unavailable email or a message without an identifiable code requires a manual response | `test_browser_challenge_fallback.py` uses the real service, resolver, challenge store, and claim store; it checks the assistance reason, exact live binding, no premature dispatch, no email claim, and cancellation |
| A reply is followed by fresh approval and one purchase | `test_gateway_challenge_checkout.py` retains the complete real-browser journey and checks that the challenge pauses the exact budget observed by the job runner |
| Pending verification is not proof of purchase | The checkout worker first observes pending state; the fixture then permits settlement, or stays pending for bounded observations and an uncertain outcome |

The gateway Chrome matrix also retains cancellation, revoked authority, automatic
email, model interpretation, a separate final purchase action, and explicit site
rejection. Do not remove a browser-specific assertion merely because an in-process
fake has a similarly named test.

## Share setup, not mutable state

CLI tests use plain output and a fixed 80-column, 25-line terminal through the
shared fixture so wrapping does not depend on the developer's terminal or CI.
Rendering tests can pass explicit console dimensions or override the environment
when terminal geometry is part of the behavior under test.

Compaction lease tests advance only the session store's clock and keep provider
work blocked until renewals are observed. They retain the real heartbeat and
fenced database commit, including renewal during commit, without requiring a
loaded host to renew within a one-second wall-clock lease. The owned-operation
success test waits for a renewal event. The challenge active-work expiry test
retains its real timer against an explicitly blocked task, so delayed scheduling
cannot turn expiry into successful task completion. Owned tasks are cancelled
and joined if a bounded wait fails.

`test_claude_code.py::test_timeout_kills_subprocess` retains a real inactivity
timer and verifies termination through the actual spawned process. It does not
depend on child-side invocation logging completing before the timeout.
The gateway conversation concurrency case starts its second conversation while
the first provider is confirmed live and blocked, preserving the overlap check
without racing two cold runtime constructions. It cancels and joins owned tasks
if either bounded startup wait fails.

Conversation, checkout, and challenge builders live in their `*_support.py`
modules. New tests should not import helpers from test modules. Keep assertions
about the behavior under test in the test itself; support providers may assert
their scripted protocol preconditions.

Ordinary tests share an empty bundled catalog. Tests that author bundles request
the isolated `bundled_root` fixture. Home directories, user data, and the crontab
guard remain isolated for every test.

Release fixtures share one per-invocation uv cache and immutable wheels under a
cross-worker build lock. Completion uses the unmodified candidate; resume and
rollback use a separate wheel with a one-shot fault tied to each installation's
own marker. All installation and database state remains per-test. No build cache
survives into a later test invocation, so working-tree edits cannot reuse stale
candidate code.

Workflow design visualization uses `test_workflow_visualization.py` for prompt and
binding fidelity, fresh-source reads, profile/export isolation, tool contracts, and
CLI compatibility. `workflow_visualization_support.py` provides one small shared
design fixture. `test_workflow_visualization_browser.py` owns the real Chrome boundary
for offline document loading, text safety, theme persistence, loop expansion, search,
edge inspection, zoom, and mobile layout. Extra parsing and policy cases belong in
the focused inspector tests rather than additional Chrome journeys.

`test_shell_lifecycle.py` owns real subprocess checks for desktop visualization
handoff through captured shell output, and cancellation/timeout after a shell
leaves a child holding its pipes. These tests use controlled local child processes;
Chrome rendering remains in `test_workflow_visualization_browser.py`.
