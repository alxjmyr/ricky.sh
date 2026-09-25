# Verification baseline 01 — September 25

Objective: pass verification independently, not eliminate challenge incidence.

This configuration demonstrated operator-completed verification followed by successful Ricky-driven search and product navigation. Individual settings have not been causally isolated. Trial 01 below established automatic clearance with supervisor-assisted follow-up.

## Fixed launch configuration

- Current uncommitted repository implementation; Google Chrome Stable, owned persistent headed session, resource `personal/ricky-personal`.
- Explicit dev environment: `XDG_CONFIG_HOME=$HOME/.config/ricky-dev`, `RICKY_USER_DATA_DIR=$HOME/.ricky`.
- `no_viewport=True`, `chromium_sandbox=True`, `service_workers="block"`, downloads enabled under the existing owned temporary path.
- Ignore only Playwright defaults `--password-store=basic` and `--use-mock-keychain`; other defaults retained.
- Owned Chrome now includes `--disable-blink-features=AutomationControlled` by default, following explicit user approval; the historical diagnostic wrapper supplied the same flag and asserted `navigator.webdriver == false`.
- Provider/model: OpenRouter / `openai/gpt-5.6-luna`.
- Existing bounded hold implementation and limits retained; fresh visual target, feedback observations, independent hard deadline, explicit release and post-release verification.

Private immutable-at-checkpoint copies of the three launcher scripts plus hashes, backend hash, and Git revision are in `~/.ricky/dogfood-evidence/2026-09-25/baseline-01/manifest.json`. These historical copies predate adoption of the indicator flag into the checkout default. Do not modify the frozen evidence. Future diagnostics should use the built-in flag without adding it a second time.

```sh
XDG_CONFIG_HOME="$HOME/.config/ricky-dev" RICKY_USER_DATA_DIR="$HOME/.ricky" \
  uv run python "$HOME/.ricky/dogfood-evidence/2026-09-25/baseline-01/ricky_visual_timing_trial.py" \
  chat --profile personal --provider openrouter --model openai/gpt-5.6-luna
```

Fresh session means a new chat and browser process using this same persistent dev profile. It does not erase cookies, reset site reputation, or isolate server-side state. If no challenge appears, record absence without claiming an automatic pass.

Next trial: no manual challenge input; Ricky performs one bounded hold, observes until explicit completion/rejection or deadline, releases, checks a fresh image, and verifies continuation with a search. Do not use Escape to dismiss Target search suggestions: operator comparison confirmed that it clears the query. Submit directly with Enter.

## Trial 01 result

Fresh chat `session_a1250944c55a400aa0d402b0c41fa23e`, terminal 38418; browser `browser_session_07930d6ca6934993823ab83bc34d8a17`, page `browser_page_cabd4d55abc14dec9bfec8d79aae9a6c`. Baseline indicator assertion passed. Browser remains open.

One automatic hold `browser_hold_733b7acbfb2147c58535b5472b1c6a0a`: mouse-down completed at diagnostic second 155.5659; automatic deadline mouse-up began 185.5667 and completed 185.5974. Three held-image captures succeeded in 0.148–0.164 seconds. Ricky initially reported failure without a post-release visual; supervisor requested the missing observation. Fresh image `media_87fdc65046594ada8715ae65c4e40888` showed clear homepage, independently viewed by supervisor.

Continuation: input ref unavailable under search suggestions, so direct Enter could not proceed. Supervisor authorized a visually identified paper-towels suggestion instead; Ricky clicked once. Final image `media_917855c043d44e999bf63c0628464cd0` shows 242 paper-towel results with an unrelated feedback modal, no verification overlay. Images/artifacts/network log preserved privately in `~/.ricky/dogfood-evidence/2026-09-25/baseline-01-trial-01/`.

Operator explicitly confirmed no interaction with the challenge or browser throughout this trial, and independently observed successful paper-towel search results. This establishes automatic challenge clearance by Ricky's input followed by verified task continuation under baseline 01. This was supervisor-assisted follow-up, not a flawless autonomous end-to-end decision loop: required fixes include mandatory post-release visual verification and avoiding premature failure conclusions. Do not infer that 30 seconds is the required hold duration or which baseline setting caused acceptance. One successful trial establishes capability, not repeatability across sessions/sites or background execution.

## Guidance repair and follow-up

All three saved held images from trial 01 show processing dots. None proves an earlier release signal. Redsky responses recover at diagnostic second 190.66, about five seconds after release; transport timing supports delayed recovery but does not explain the vendor's acceptance decision.

Updated hold result guidance is state-specific: observe visually while holding, do not treat processing as completion, and explicitly call `browser_visual_snapshot` after release before concluding clearance/rejection. Deadline expiry is not a verification verdict. This is model guidance, not a deterministic outcome gate; input deadlines and cleanup remain enforced.

The live background-model fixture exposed contradictory named-job briefing (“navigate and observe only”) despite authorized hold tools. Updated `jobs/briefing.py` explicitly describes the existing bounded verification exception; capabilities, permissions, and budgets are unchanged. Focused tests retain original effect/accounting assertions and verify the briefing and post-release visual step.

Real model + real headless Chrome + actual JobRunner controlled trial succeeded without follow-up coaching: hold began 15.933s, fixture indicated ready 17.133s, model released 23.025s, delayed page completion 24.023s. It visually verified result code `cobalt`, recorded completed, and persisted both hold effects as performed with zero ordinary effect calls. This proves background execution on a controlled fixture, not headless Target acceptance. An earlier fixture omitted `type=button` and was correctly rejected as consequential; after correcting fixture semantics, the next run refused because of the briefing conflict. Both failures are retained as evidence.

Target repeat with unchanged browser settings and updated hold guidance: chat `session_4dd54b0eb0fc466686c5726985c4b096`, terminal 41268. A broad search request did not establish visual clearance. Follow-up requested one reload, visual inspection, and at most one automatic hold without release/outcome coaching. Hold `browser_hold_344078b245f44477ad28b7fe9f01585b` pressed at 358.631s and deadline-released at 388.6642s. Held images show processing; final original-resolution image `media_d443db09f90441498ec7f4d359dd80cf` clearly says “Please try again.” Ricky took the post-release visual and correctly reported failure without supervisor correction. Exact target was “Press & Hold Human Challenge,” not the neighboring accessibility button. User was asked for one manual attempt in the same window without reload; response pending.

Private follow-up archive: `~/.ricky/dogfood-evidence/2026-09-25/guidance-followup/` includes Target images/artifacts/network checkpoint, controlled background script/logs, successful worker transcript, and first validation log. Initial full validation: 2965 passed, two failed (stale generated docs and gateway multi-handoff assertion). Docs refreshed; both isolated checks passed unchanged. Final full-suite retry is pending.

Operator follow-up: manual hold in the same rejected page state also failed with “Please try again.” This does not isolate a Ricky input defect or establish a vendor reason; it is not a fresh-challenge comparison.

Two additional actual background-model fixture runs passed: processing-only feedback with a model release at 26.69s (not deadline, and not proof it followed the release guidance perfectly); then an explicit fixture-only 3s deadline, runtime release at ~3.006s, delayed completion one second later, and final visual verification without coaching. The latter persisted the start effect; release was runtime lifecycle cleanup, not a model release call. These fixtures used isolated ephemeral headless browsers and did not change baseline 01 or the live dev profile. Scripts, run logs, and transcripts are in the follow-up archive.

Actual background Target comparison: one temporary named job through JobRunner, same dev persistent resource and baseline indicator flag, headless enabled only in the process-local settings copy. No saved profile configuration changed. Job `jobrun_f2222083cecd4cc1ae16d3dfe37a7f2e` / session `session_9a190a3942984da68185fdb16576abd5` opened and navigated successfully, encountered a semantic timeout, obtained a visual challenge observation, started one hold, observed once, released after 5.6296s, then received `visual browser snapshot failed`. Model reported blocked, but there is no successful final image confirming rejection or clearance; classify verification outcome as unresolved. Runtime cleanup deleted PNGs before archival; one text artifact and transcript/network log survived and are archived. No independent pixel assessment of this trial is possible. The temporary named job definition was removed, runtime closed, process exited 0. Controlled fixture successes do not substitute for Target headless acceptance.

Owned headed diagnostic browser and chat were closed after evidence preservation; patched diagnostic logger shutdown exited 0. No active dogfooding browser is retained. The logger fix is diagnostic-only repeated-listener cleanup, not a browser launch change.

Final validation: **2967 tests passed in 444.97s**, including browser/release lanes, with `uv run pytest -n 2`. Ruff and Pyright passed; `git diff --check` passed. No assertions weakened or tests excluded. The first full run's gateway handoff failure passed unchanged in isolation and the complete retry; root cause remains unestablished. Final log retained as `ricky-verification-guidance-tests-final.log` in the follow-up archive.

Configuration adoption decision (updated after explicit user approval): make the baseline indicator flag the default for all Ricky-owned launches, headed/headless and ephemeral/persistent. Preserve native credential-store selection, sandboxing, existing profile data, other Playwright defaults, and blocked service workers. No new setting or profile migration; manual setup and external CDP attachment remain unchanged. Successful manual and automatic trials support retaining this baseline but do not isolate causality or guarantee repeatability. Model observation/release mistakes and unresolved headless visual failures remain separate follow-up work. Validation of this adoption: `uv run pytest -m browser_integration -n 2` passed all 41 tests in 107.78s; `uv run pytest -n 2` passed all 2967 tests in 360.05s; Ruff and Pyright passed. The existing real Chrome navigation journey now directly asserts `navigator.webdriver is false`. Logs: `/tmp/ricky-indicator-browser-tests.log` and `/tmp/ricky-indicator-full-tests.log`. No new live Target trial was performed for this adoption.
