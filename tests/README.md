# Test ownership and local execution

See [the development guide](../docs/development.md#run-tests-locally) for local
commands. `uv run pytest` includes every lane. The core lane excludes only real
Chrome and released-installation drills; it retains real stores, transactions,
leases, recovery, provider adapters with fake transports, and tool policies.
The browser boundary file has one `xdist_group` to avoid running several large
DOMs and process-failure drills simultaneously. Other tests use small scheduling
batches, including the independent release recovery scenarios.

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
