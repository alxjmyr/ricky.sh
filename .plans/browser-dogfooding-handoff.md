# Browser dogfooding handoff — September 24, 2026

Start here tomorrow. This is the current checkpoint; the [detailed investigation](browser-readiness-and-challenge-dogfooding.md) retains the full chronology, identifiers, and evidence. Work is unfinished. Do not treat successful local tests or one available homepage as proof that Target verification is solved.

## Where we stopped

### September 25 morning resume

- **Latest decision:** user approved adopting `--disable-blink-features=AutomationControlled` as the default for all Ricky-owned launches. Implemented and validated: 41 browser tests and all 2967 full-suite tests passed; Ruff and Pyright passed. Prior experimental-only recommendations below are superseded. See the baseline document. No active diagnostic browser remains.

- **Final validation for guidance repairs: 2967 passed in 444.97s; Ruff, Pyright, and diff whitespace checks passed.** Final test process completed. No active diagnostic session remains. Baseline follow-up file records remaining Target reliability and observation limitations; do not mark broader verification objective complete.

- **Latest follow-up checkpoint:** see [baseline follow-up](browser-verification-baseline.md) for current results. New code makes hold guidance state-specific and corrects contradictory background job briefing; no launch settings or authority/schema changes. Actual model + real headless Chrome + JobRunner passed three controlled fixture cases (explicit release feedback, processing-only, and automatic short deadline with delayed completion). Target repeat failed visually with “Please try again”; operator's manual attempt in that same state also failed. A separate actual headless Target worker attempted one hold but post-release visual failed, so outcome remains unresolved. All diagnostic browsers/chats are now closed; patched logger cleanup exits 0. Final full validation pending in `/tmp/ricky-verification-guidance-tests-final.log` (terminal 65553), plus final lint/type checks already passing. Archive `~/.ricky/dogfood-evidence/2026-09-25/guidance-followup/`. Keep baseline experimental; additional reliability work remains.

- **Current milestone: confirmed automatic clearance.** See [locked baseline and fresh-session trial](browser-verification-baseline.md). User explicitly confirmed zero browser/challenge interaction during fresh-session trial 01 and visually confirmed successful paper-towel search. Ricky supplied the single hold; deadline released at ~30 seconds; supervisor prompted missing post-release visual verification and alternate suggestion-based search continuation. Baseline remains fixed; next priority is reliable feedback/release/outcome decisions and repeatability, not more flag experiments. Live terminal is now 38418, chat `session_a1250944c55a400aa0d402b0c41fa23e`; earlier terminal 24058 exited with diagnostic listener-removal KeyError. Evidence is archived before cleanup.

- Local isolated headless Chrome diagnostic ran successfully (exit 0): a plain Search button covered by a fixed overlay, invoked through Ricky's `_dispatch_action`, raised PlaywrightTimeoutError while its click counter remained **0**. The production `perform_action` catch uses “timed out after dispatch” for this exception too. Confirms error wording can imply more than the evidence supports; does not prove this was Target's exact failure mechanism. Local browser closed in finally; live Target profile untouched. Next useful operator comparison: manually type a query and press Escape to distinguish Target's native focused-input behavior from Ricky targeting Escape at the suggestions dialog.

- User independently confirmed the final product page shows up&up Make-A-Size, 12 triple rolls, without a verification overlay. The manual-clearance → agent search → product-page sequence is operator-confirmed. Leave this live page open while investigating timeout diagnostic ambiguity locally.

- **Successful continuation after manual clearance:** refilled and submitted Enter directly on search input without Escape. Commit succeeded, navigation generation 3; image `media_36284a862b1d4d35bb17683b6d7106c6` shows 242 paper-towel results including Bounty/Brawny, independently viewed by root. No challenge. Then opened a clearly labeled up&up Make-A-Size Paper Towels product link once; action `browser_action_fb3fa70aa8b2487190a6629e2961bbb0` succeeded. Ricky reports usable product detail page, no challenge/errors; image `media_a0ce6352b97040b6aafeebcd0fcf0e3f`. Both images archived privately. No cart/account changes. This establishes manual clearance followed by successful Ricky-driven search and product navigation; automatic challenge clearance still unproven. No production code changed today.

- Controlled query-loss check: fill action `browser_action_9aa5a4f190e5448cb6e5a4090bb28b39` succeeded; user independently confirmed `paper towels` visibly present. Then only Escape on the Search suggestions dialog (ref `f41e2668`, action `browser_action_2ac755a321c44ff1ab2a07daa630752e`) succeeded without navigation. User confirmed the field became blank with blinking cursor. Thus this exact Escape sequence clears the query; do not treat it as a harmless dismissal. Next trial refills and submits with Enter directly, without Escape.

- Latest continuation: user explicitly confirmed manual clearance and homepage. Ricky's fresh visual succeeded (4.013s); it filled `paper towels`, dismissed Search suggestions with Escape, then activated labeled search ref `f41e50` once. `browser_commit` returned **ok**, reporting navigation and generation 2. However, post-action image `media_dd2791e1735e4c139c3fdb40d8f98bc0` showed the Halloween homepage, independently inspected by root. A later read-only semantic + visual check also showed homepage, URL `https://www.target.com/`, no challenge, no paper-towel results; image `media_2d5a5faa0d0644b0877dc254967762fe`. Search value not exposed in semantic result. Do not equate the successful action receipt with successful search. No further submission. Current session images and updated network checkpoint preserved in September 25 private archive. Next investigate whether Escape affected the query or form submission returned home; these are hypotheses, not confirmed causes.

- New observed dev chat: `session_da264281c3aa49319700931e32f44c73`, terminal 24058. Network diagnostic: `/tmp/ricky-network-20260925T133933Z.jsonl`. Browser session `browser_session_b89fc09bde114fe6ba9853f2bab4bb3d`, page `browser_page_bc10dc68e3854fddb066c801cad9e235`. Browser/chat remain open at this checkpoint.
- Initial visual succeeded on the third call (earlier calls returned errors); Ricky reported homepage/sign-in popover/no challenge, and the user independently confirmed. Image `media_a2fae249a8654c348a557fc87f03724a`.
- Filled `paper towels`, dismissed Search suggestions with Escape, and activated the explicitly labeled `search` button (ref e50) once via `browser_commit`. Action returned an error; Ricky reported `operation_timeout`, outcome uncertain after dispatch. No submission retry.
- Subsequent visual succeeded in 3.815 seconds, image `media_c80d035707a5484a91cf96b58fb5f33b`. Ricky correctly reported Quick verification / Press & hold, no matching results visible, and stopped. User independently reported the challenge while that observation was pending. No challenge interaction or manual clearance in this trial yet.
- This reproduces a challenge with the labeled search button too; the prior unnamed suggestion link is not necessary for challenge incidence. It does not establish Target's reason. Detection happened in a separate visual observation after the action error, not continuously during the action. Next: preserve images and examine action timing/observation behavior before further input.
- Follow-up correction: action artifact `artifact_360f9f905d444f8186d293432166480a` reports `in_doubt`, `operation_timeout`, `outcome_uncertain=true`, and `navigation_occurred=false`. No lower-level call phase is retained. Code catches a timeout from `locator.click()` itself with the same “after dispatch” message: receipt of the click/search submission is **not confirmed**. Overlay interception during click actionability remains a hypothesis, not an established cause. Root independently viewed the challenge image. Both morning images, action artifact, and a network checkpoint are preserved privately under `~/.ricky/dogfood-evidence/2026-09-25/`. Proposed next live step: user manual clearance, fresh visual confirmation, then controlled continuation.

- The user manually passed Target's challenge in the same Ricky-controlled dev browser. The user then intentionally closed the browser. No diagnostic Chrome or chat is currently owned.
- The last chat was `session_82897fbebcac46928002270555ca1e94` (terminal 76835). `/quit` ended with exit 1 and a cleanup traceback; the primary exception was not retained in bounded output. A subprocess finalizer reported `Event loop is closed`. This needs isolation from the temporary diagnostic wrapper.
- All implementation work remains in the current worktree, **uncommitted**, including new hold modules/tests and the active plans. No commit, merge, or deployment was performed. Preserve the user's unrelated `.designs/tool-templates.md` edits.
- Latest change: each tool-produced image now has an adjacent image ID, tool name, and call ID label. Its live benefit is unproven.
- Full validation of the latest change passed: **2,964 tests in 449.14 seconds**, including browser and release lanes. Ruff and Pyright also passed. The completed log is preserved in the private archive below. No validation run remains active.

## Most important learnings

1. **Manual verification can pass under Ricky's current controlled launch.** It is not an absolute environmental block. This does not prove automated input is accepted consistently, or establish which browser flags matter.
2. **Challenge incidence, completion, and continued usability are separate outcomes.** Target can display a normal homepage and then challenge on search/reload. Most launch comparisons measured incidence only; they did not attempt a hold.
3. **Semantic content can describe the homepage hidden underneath a challenge.** Never infer clearance from semantic controls, an unchanged URL/title, or absence of challenge text. Require current visual evidence and successful task continuation.
4. **Several genuine observation defects were repaired.** Serial inspection hit a 10-second limit after 57–73 controls. Bounded concurrency improved settled captures; pinned elements avoid waiting for vanished indexed locators; capture retries once with fresh masks when a frame detaches. Recent live captures succeeded in 4.09–6.79 seconds.
5. **Model interpretation is also fallible.** After the user cleared verification, the saved latest image showed the homepage, but Ricky reported a challenge. Without another screenshot, explicitly identifying the latest image made Ricky describe it correctly. New labels clarify provenance; they are not a proven fix. A four-request synthetic comparison passed both with and without labels.
6. **One automatic hold was released prematurely.** Ricky saw one unchanged frame and released after about 9.09 seconds; the post-release image showed “Please try again.” A separate attempt with explicit continuation instructions reached the 30-second deadline. Immediate outcome captures failed, but product-data HTTP 200 responses followed about seven seconds after release and a later image showed the homepage. Manual intervention in that earlier interval was never established: do not call it an unqualified autonomous pass.
7. **The latest search re-triggered verification.** Filling `paper towels` succeeded; clicking an unnamed suggestion timed out. A successful subsequent visual showed the challenge, independently confirmed by the user. The user then passed it manually. A planned comparison using the explicitly labeled search button never ran because the user had closed the window.
8. **Network transport success is not verification success.** Collector HTTP 200 responses carried opaque payloads. Redsky HTTP 435 correlated with blocked product-data requests; later HTTP 200s support recovery but do not reveal the vendor's decision reason. No evidence establishes a ban, rate limit, cooldown duration, or that the experimentation itself caused rejection.

## The three requested compatibility paths

| Path | What was tested/found | Current implementation status |
| --- | --- | --- |
| Automation indicators | Temporary launch flag `--disable-blink-features=AutomationControlled` verified `navigator.webdriver=false`. Challenges still appeared; latest manual pass occurred with this variant. | **Experimental only** in diagnostic launcher. No production stealth/default change. Need completion + continuation comparisons, not incidence alone. |
| Ordinary Chrome defaults | Minimal Playwright, reduced default flags, ordinary Chrome with nonzero CDP port, delayed attachment, reload, and detach comparisons. Delayed attachment preserved an already-loaded homepage, but reload reintroduced verification. Ordinary Chrome later also challenged and manual verification passed. | No default overhaul adopted. Ordered same-profile trials share cookies/server state; they do not isolate a single cause. |
| Service workers | Allowing workers did not eliminate challenges in the minimal comparison. Local probes found a real navigation-guard gap involving persisted workers/cached redirects and popups; even existing `block` does not neutralize previously installed workers. | Keep current behavior for now; do not blindly allow workers. A partial CDP bypass fix was **reverted** because cached popups could execute before interception. Failing regression preserved in [browser-service-worker-regression.patch](browser-service-worker-regression.patch), not applied to the active suite. |

## Implemented work to retain

- Foreground **and background** automatic verification capability, separate from ordinary interaction/transaction authority.
- Hold targets use exact fresh visual candidate references; native hover handles nested frames before stationary mouse-down. No arbitrary model coordinates or dragging.
- Defaults: 30-second independent hold deadline, two attempts per page generation, 60 reserved seconds per runtime. Joined release on cancellation/shutdown, failure handling, durable effect receipts and background accounting.
- During a hold, images are masked observation-only captures; no actionable candidates or stable-image requirement. Late observations cannot become actionable after release.
- Main-frame offscreen prefilter, batches of at most eight read-only candidate inspections, DOM-ordered output, owned task cancellation/joining.
- Pinned candidate element handles with disposal and identity recheck before retaining action locators; one masked recapture on actual frame detachment under the same deadline.
- Latest per-image provenance labels in `src/ricky/agent/loop.py`.
- Contracts/docs: `.designs/browser.md`, `docs/browser-control.md`, `ricky.toml.example`; coverage map: `tests/README.md`.

New production files: `src/ricky/browser/holds.py`, `hold_service.py`, `hold_tools.py`. New tests: `tests/test_browser_holds.py`, `tests/test_browser_hold_job.py`. Other modifications are listed in `git status --short` and the private backup manifest.

## Validation checkpoint

- **2,964 tests passed in 422.07 seconds** for the capture-repair revision, including browser and release lanes; Ruff and Pyright passed. Log: `ricky-capture-full-tests.log` in the archive below.
- Latest image-label change: focused ordered-media regression, Ruff, and Pyright passed. An initial full run was terminated with exit 143 at 63%; the full retry passed **all 2,964 tests in 449.14 seconds**. Log: `ricky-image-label-full-tests-retry.log` in the archive.
- Earlier runs also had unexplained SIGTERM termination and one native worker crash in manual Chrome setup. Successful complete reruns exist. A capture-test wrapper TypeError was corrected without weakening assertions. No test exclusions or timeout relaxations were used.
- Final full retry uses a persistent terminal, two workers, and retained verbose logs. Cause of external termination remains unknown; different runs stopped at different tests.

## Tomorrow's starting sequence

1. Read this checkpoint and the final validation result; inspect `git status`. Do not reset/recreate the worktree or rerun already-passing gates unless code changed or evidence warrants it.
2. Start the preserved diagnostic launcher below. It loads the current code (including image labels), confirms dev data root, times capture stages, records redacted network shapes and mouse timing, and records page/context closure events. No production profile should be touched.
3. Open Target once, obtain a fresh visual, and record its state. Tell the user exactly when any manual assistance is needed. Explicitly record who acted so clearance can be attributed.
4. First practical comparison: search for `paper towels` using the **explicitly labeled search button**, with a fresh ref after filling. No cart/account changes. If blocked, observe and stop rather than repeating input.
5. For a new automatic hold trial, use the existing attempt/deadline limits. Explicitly instruct continued visual observations while feedback is unchanged, release on confirmed completion/rejection or observation failure, and verify both clearance **and subsequent search**. A checkmark, HTTP 200, or mouse-up alone is not success. If interpretation disagrees with the operator, inspect the exact latest saved image at original resolution.
6. Capture outbound image IDs/digests/order if interpretation still disagrees; current source review and a later correct reread do not prove what reached the earlier request. Do not log image base64, keys, cookies, or raw private payloads.
7. Only after a controlled end-to-end result, decide which launch changes to adopt. Preserve the original three-path scope. Worker support still needs its interception/ownership problem resolved; use an isolated checkout for the saved failing regression.
8. Reproduce the diagnostic shutdown error on a local ephemeral browser with and without the wrapper before changing core cleanup. Keep it separate from site rejection.

## Exact dev launch (important)

Tool shells do **not** load the user's interactive `uv` wrapper from `~/.zshrc`. Running `uv run ricky` in the repo alone previously selected the wrong data root. Always supply both overrides:

```sh
cd /home/alex/Developer/ricky.sh
XDG_CONFIG_HOME="$HOME/.config/ricky-dev" RICKY_USER_DATA_DIR="$HOME/.ricky" \
  uv run python "$HOME/.ricky/dogfood-evidence/2026-09-24/scripts/ricky_visual_timing_trial.py" \
  chat --profile personal --provider openrouter --model openai/gpt-5.6-luna
```

The persistent resource is `personal/ricky-personal`, rooted in dev `~/.ricky`. Production is `~/ricky_prod`; do not use or close its gateway. Dev Target was last visually signed out. Successful human verification does not imply signed-in state. CLI PTY input submits with `\r`; `/quit\r` closes the owned chat/browser normally. Browser open and bounded task actions are already authorized within this dogfooding scope; honor Ricky's actual permission prompts.

## Preserved evidence and work backup

Private archive: `/home/alex/.ricky/dogfood-evidence/2026-09-24/` (private permissions).

- `scripts/`: network/timing/indicator launchers, Chrome comparison script, synthetic image-order probe, worker probes. Cross-script references were rewritten to this archive; no `/tmp` dependency for those imports. They still write new diagnostics to `/tmp`; archive outputs after each session.
- `logs/`: redacted network JSONL and retained test logs. Most relevant: `ricky-network-20260925T015250Z.jsonl` (automatic hold attempts) and `ricky-network-20260925T022055Z.jsonl` (search challenge, manual pass, intentional closure).
- `captures/`: eight Chrome launch-comparison directories with retained screenshots and observation logs.
- `worktree/`: base commit, git status, tracked binary patch, and a tarball of changed/new source, tests, docs, and plans. This is a backup, not a commit; do not blindly apply it over newer work. It also preserves the user's existing tool-template edit.
- `SHA256.json`: integrity manifest.

Some runtime-retained Ricky screenshots were removed by session cleanup before archiving. Their exact IDs, independently inspected visible contents, timings, and user confirmations are recorded in the detailed investigation and this conversation; do not claim those PNG bytes are in the archive. Future experiments should preserve needed captures **before** session cleanup. No live profile, credentials, cookies, or `.secrets.toml` was copied into the repo or evidence archive.
