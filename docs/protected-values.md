# Use protected values

Ricky can fill recognized browser credential, payment, and one-time-code controls without sending
the raw value to the configured model provider. The model sees a profile-qualified alias, safe
field labels, destination policy, and browser target metadata. Only Ricky's local protected-value
broker releases one exact field to the dedicated browser backend.

Protected values are independent from infrastructure credentials in `.secrets.toml`. Provider API
keys, OAuth client secrets, and transport tokens remain configuration inputs; they are not listed
or materialized by agent tools.

## Enable and initialize a vault

Enable the installation-owned subsystem in `<user_data_dir>/ricky.toml`:

```toml
[protected_values]
enabled = true
```

Initialize each profile that will own protected values:

```bash
ricky protected-values init --profile personal
ricky protected-values status --profile personal
```

The hidden prompt creates a random profile data key and wraps it with a key derived from your
passphrase. The unlock slot records the exact KDF parameters used to wrap that key, so changing
installation KDF settings does not make an existing vault unreadable. New settings take effect
when you initialize a vault or rotate its passphrase. Ricky does not store the passphrase beside
the vault. A foreground chat asks for it locally when the vault first needs to materialize a stored
field. Closing the runtime drops the unwrapped key.

The vault works in a desktop terminal, headless host, or SSH TTY. A foreground chat prompts when
it first needs a stored value. A gateway never prompts through Telegram or another messaging
transport and does not fall back to a prompt after startup. You can instead unlock selected vaults
locally when you start the gateway, as described below. Standalone jobs and scheduled processes
cannot borrow the gateway's unlocked state.

## Unlock selected vaults for a gateway process

Pass a profile name when you start a foreground gateway:

```bash
ricky gateway run --unlock-vault personal
```

Repeat the option to unlock more than one profile:

```bash
ricky gateway run \
  --unlock-vault personal \
  --unlock-vault work
```

Ricky takes the single-instance gateway lock before prompting. If another gateway already owns
the same `user_data_dir`, the command fails without asking for a passphrase. Ricky verifies that
every requested profile is enabled and initialized, collects all passphrases through local
no-echo prompts, and unlocks the set as one startup operation. If any prompt or unlock fails,
Ricky relocks every requested vault and does not start the gateway.

For a managed Linux service, use the same repeatable option on `start` or `restart`:

```bash
ricky gateway service start --unlock-vault personal
ricky gateway service restart --unlock-vault personal
```

`service start --unlock-vault` rejects an already active gateway before prompting. Use `restart`
when you intend to replace it. The invoking CLI passes each entered passphrase once to the exact
new gateway through an owner-only, one-use local socket. It does not put passphrases in systemd
arguments, environment variables, unit text, configuration, logs, or regular files.

An unlocked gateway retains the unwrapped vault key in process memory, not the passphrase. The
unlocked state lasts until that gateway exits. Starting or restarting without `--unlock-vault`, an
automatic service restart, a crash, or process replacement starts a locked gateway. To restore
access, restart it locally with the option again.

Startup unlock controls vault availability only. It does not widen a gateway route's profile
scope, grant a capability, approve a destination, or authorize an effect. Every consumer must
still satisfy its own resource, destination, revision, field, budget, and authority checks.

## Create resources

Aliases, labels, descriptions, field labels, and policies are safe metadata and may reach the
model provider. Do not put account numbers, usernames, card digits, or other secrets in them.

Create a credential with the kind-specific username and password fields:

```bash
ricky protected-values add personal/example-login \
  --kind credential \
  --label "Example login" \
  --origin https://accounts.example.com
```

Ricky confirms the safe descriptor, then reads each stored value through a no-echo prompt. Values
and passphrases are never command arguments, options, shell-history entries, or non-TTY stdin.

The default `payment_card` schema stores cardholder, card number, and expiry fields but prompts for
the card security code on every use. The default `one_time` schema also prompts on every use. Use a
repeated safe field descriptor when a site needs a different shape:

```text
--field name:control:mode:label
```

Ricky accepts `stored` and `prompt_each_use`. One-time-code fields must remain
`prompt_each_use`. Card-security-code fields default to `prompt_each_use`, but you can explicitly
author one as `stored` when unattended card use is required. The resource remains unavailable to
background execution until you separately enable its unattended policy and exact ceilings.

Inspect or maintain resources without a model provider:

```bash
ricky protected-values list --profile personal
ricky protected-values show personal/example-login
ricky protected-values update personal/example-login --label "Primary example login"
ricky protected-values disable personal/example-login
ricky protected-values delete personal/example-login
ricky protected-values rotate-passphrase --profile personal
```

Updating stored values requires `--replace-values` and fresh hidden entry. Deletion removes the
current ciphertext and approvals but retains safe use evidence; encrypted backups can retain older
bytes.

## Choose destination policy

Protected policy is independent from ordinary browser destination policy:

- `strict` permits only exact authored origins.
- `confirm_new` prompts locally to deny, allow once, or durably approve an exact top-level and
  target-frame origin pair.
- `approved_only` permits authored and already approved exact origins without expanding policy
  during a chat.
- `secure_web` permits public HTTPS origins. Ricky checks resolved addresses when materializing
  and again before dispatch; browser private-origin exceptions cannot widen it. It never permits
  plain HTTP or private-network destinations.

Origins are exact scheme, host, and port values. Ricky does not infer parent domains, related
organizations, or wildcard subdomains. Inspect and change policy or approvals with:

```bash
ricky protected-values policy show personal/example-login
ricky protected-values policy set personal/example-login \
  --mode strict \
  --origin https://accounts.example.com
ricky protected-values approvals list personal/example-login
ricky protected-values approvals revoke personal/example-login \
  --top-origin https://accounts.example.com \
  --frame-origin https://accounts.example.com
```

The `approvals approve` command is available for an explicit operator decision. Page content and
model tool arguments cannot create an approval.

## Use a protected value in browser chat

Ask Ricky to use a qualified alias and safe field name. Ricky takes a current browser snapshot,
derives the live protected control category and both origins locally, applies broker policy, and
asks for ordinary external-effect permission. The review never includes the raw value.

One call fills one field and consumes the snapshot. Filling can trigger page JavaScript or
autosave, so it is never automatically replayed after dispatch may have begun. Filling does not
submit, click, press Enter, or authorize a later transaction. A consequential browser commit
remains a separate destructive review.

When a later financial commit uses a payment method filled on the current page, its approval may
name that protected resource by profile-qualified alias. The browser retains only bounded,
non-secret current-page use evidence; it never adds the value, digits, username, or derived hints
to the transaction envelope. Payment methods already stored by a website use a clearly labeled
model-proposed source description instead. See
[Browser transaction approvals](browser-transactions.md).

Ordinary browser fill, key entry, and coordinate clicks still reject recognized protected
controls. CAPTCHA, passkey, SSO, unsupported protected controls, and ambiguous interfaces still
use local browser handoff.

## Use protected values in a gateway browser execution

Enable `browser.background.protected_values_enabled`, the
`builtin.protected_value.use` background guardrail, and
`authority.capabilities.protected_value_use`, then start the gateway locally with
`--unlock-vault PROFILE`. The execution contract pins each allowed alias, revision, field, and
materialization limit. The broker independently checks the current browser control, exact HTTPS
top-level and frame origins, destination policy, execution identity, and per-execution use count.

Protected resources are unattended-forbidden by default. Replace one resource's policy with exact
destination and execution ceilings before selecting it in a background contract. For example, a
card limited to one USD commit of at most $250 and four field materializations uses:

```bash
ricky protected-values policy set personal/example-card \
  --mode confirm_new \
  --origin https://checkout.example.com \
  --allow-unattended \
  --max-unattended-materializations 4 \
  --allow-unattended-commit \
  --max-unattended-commits 1 \
  --max-unattended-amount-minor 25000 \
  --unattended-currency USD
```

`policy set` replaces the complete policy. Omitting the unattended flags and ceilings restores the
safe unattended-forbidden state. The amount uses minor currency units; for USD, `25000` is $250.

An unattended `confirm_new` destination creates a source-bound one-use gateway approval. It never
creates a durable destination approval. The notification includes an approval ID and one-time code;
reply with the exact `/approve` or `/deny` command from that message. Page, snapshot, target, or
origin drift invalidates the occurrence before materialization.

If a financial commit depends on a protected funding source, the protected resource must also
explicitly allow unattended commits and define its commit and amount ceilings. Ricky reserves that
source policy after the transaction approval is consumed and before browser dispatch. A performed
or ambiguous dispatch consumes the commit allowance. The user-approved exact amount and currency,
the owner authority ceiling, and the protected-resource ceiling all apply; none is a substitute for
the others.

Prompt-each-use fields and ordinary OTP values remain unavailable unattended. An explicitly stored
card security code can participate only when that field and resource separately allow unattended
materialization and commits. Named and scheduled jobs cannot use the resident gateway broker in
these runtimes.

## Understand the security boundary

The profile-local SQLite vault contains safe catalog metadata and authenticated-encrypted payload
blobs. A random data key is wrapped in a versioned passphrase unlock slot; passphrase rotation
rewraps that key rather than re-encrypting every resource. The vault and directories are
owner-only on POSIX and never live in project `.ricky/`.

Raw values do not enter model requests, tool arguments or results, canonical history, session
events, permission summaries, screenshots, semantic snapshots, media, artifacts, effect
identities, approvals, or audit records. This boundary does not protect a value after an approved
site receives it, or against a compromised local account, OS, browser, approved destination, or
authorized Ricky process after unlock. Python also cannot guarantee memory zeroization.

Back up the owning profile directory as sensitive encrypted data. A copied vault is protected by
the passphrase while locked, but deletion from the active database does not erase historical
backup copies.
