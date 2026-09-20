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
