# Review browser transactions

Ricky requires a fresh exact approval before it activates a consequential browser control. The
review describes both Ricky's proposal and the exact browser occurrence it will use. Approval
applies once; it cannot be remembered or reused after the page, target, destination, or proposal
changes.

An interactive resident chat uses its local terminal prompt. A gateway-owned ad hoc execution can
park the same live browser occurrence and request a source-bound Telegram approval. Named and
scheduled jobs remain read-oriented, and workflows cannot use browser tools.

## Approve a parked gateway transaction

The background worker navigates and prepares the exact commit once, then enters
`awaiting_transaction_approval`. It retains its browser, page, resource lease, claim, and in-memory
prepared effect. It performs no model or browser work while parked.

The gateway sends the complete transaction envelope, live HTTPS origins, known destinations,
semantic or coordinate target mode, exact browser resource identity and revision, pinned model
provider, execution budget ceiling, protected aliases and revisions, approval ID, expiration, and a
one-time correlation code. Approve or deny from that same authenticated conversation:

```text
/approve browser_transaction_<id> <one-time-code>
/deny browser_transaction_<id> <one-time-code>
```

A bare `yes`, `no`, or reaction cannot decide a browser transaction. The code is an intent and
correlation check, not a second authentication factor. The configured bot account, sender
allowlist, gateway route, principal policy, and owner ceilings remain the authentication boundary.

Approval wakes only the owner of that exact occurrence. Ricky rechecks the execution claim,
contract, authority, policy, resource revision, page generation, snapshot or screenshot, target,
origins, destinations, protected source, and envelope before reserving and dispatching the same
prepared effect. Drift returns `not_performed` and requires a new proposal.

If the gateway, browser, worker, resource lease, or in-memory preparation is lost, the approval is
invalid. Ricky does not rebuild the cart or form and apply the old approval after restart. Expiry,
denial, and cancellation also drop the prepared effect without dispatch.

## Understand the two approval types

Ricky uses one of two envelopes for every browser commit.

### Financial transaction

A financial transaction is any commit that can create an immediate or future payment, charge,
transfer, withdrawal, deposit, subscription, bid, paid reservation, donation, or other monetary
obligation. Its review includes:

- the exact proposed total and currency;
- the merchant, payee, recipient, or destination account;
- shown line items, taxes, discounts, and fees;
- whether the charge is one-time or recurring, including the recurring amount and cadence;
- the protected-value alias or safe label identifying the funding source;
- other material consequences; and
- the browser-visible result Ricky expects.

Ricky does not approve an unknown or open-ended total. If a site can add an undisclosed fee, choose
an unbounded tip, or apply unknown future pricing, complete the commit yourself through local
browser handoff.

A free trial that automatically becomes paid is financial even when today's charge is zero.
Entering a payment field is preparation and has its own protected-value permission; activating the
final purchase or subscription control requires the separate financial approval.

### Non-financial browser transaction

Every other consequential commit uses one generic browser transaction review. It states:

- what Ricky intends to do;
- which site, organization, account, or recipient it affects;
- the material consequences;
- data, declarations, consents, or attachments being submitted; and
- the browser-visible result Ricky expects.

This envelope covers actions such as submitting an application, sending a message, accepting
terms, publishing content, creating a free reservation, or submitting a non-financial form. Ricky
does not create a different approval schema for each kind of website.

The panel labels this approval as non-financial. Reject it if the proposed action can move money or
create a monetary obligation. Ricky also uses local payment signals to require a financial
envelope when it recognizes them, but arbitrary websites cannot be classified perfectly.

## Read the trusted review

The panel separates two kinds of information:

- **Ricky's proposed transaction details** come from the conversation and untrusted webpage
  content. Review them as a proposal; Ricky cannot prove that a merchant's displayed amount or a
  page's instructions are truthful.
- **Locally verified browser binding** identifies the current browser resource, page and snapshot,
  top-level and frame origins, target or visual coordinate, activation, dialog behavior, and any
  link or form destination Ricky can inspect before dispatch.

The local binding shows the exact profile-qualified persistent resource and configuration digest,
or a one-way resource occurrence digest for an ephemeral browser. It also shows the resource kind,
pinned model provider, page generation, one-way session and page occurrence digests, snapshot and
target digests, and the browser execution's complete budget ceiling. The digests correlate the
approval with the resident browser occurrence without exposing raw runtime identifiers.

For a visual-coordinate fallback, the review also shows the harness-issued fallback reason, exact
fractional CSS coordinate, masked-image and visual-snapshot digests, viewport dimensions,
fractional scroll position, image-to-viewport scale, nested hit-target digest, and semantic
resolution digest. These are locally produced binding facts, not a model explanation that
coordinates were necessary.

Some modern controls send requests through page JavaScript and do not expose a static link or form
destination. Ricky calls that out as `controlled by page JavaScript (not statically known)` instead
of inventing a destination. The current page and frame origins must still be exact and allowed, and
normal redirect and popup checks continue after dispatch.

If any known binding changes while the prompt is open, Ricky does not click. It reports the review
as stale, takes no transaction authority from the earlier answer, and requires a new snapshot and
approval envelope.

For a link or form with a known destination, Ricky also checks the first navigation or popup
request against the reviewed destination. A page script cannot rewrite that destination during the
click and silently reuse the approval. JavaScript-only destinations that cannot be inspected are
called out as unknown and remain subject to ordinary browser destination policy.

## Identify a funding source safely

When Ricky filled a payment method from the protected-values vault on the current page, the
financial review names only its profile-qualified alias, such as `personal/travel-card`. It does
not show the card number, account number, security code, username, or a hint derived from protected
material. If one payment field is overwritten with a different protected payment method, Ricky
uses only the latest definitive field evidence. Conflicting protected payment aliases stop the
commit and require the payment fields to be reviewed again.

For a funding source already stored by the website or entered during local handoff, the review can
use a safe label. That label may repeat a masked identifier already displayed by the site when
needed to distinguish saved methods. It is model-proposed page content, not protected-vault
evidence. Ricky rejects labels that contain recognizable unmasked card numbers, account numbers,
IBANs, or card security codes.

## Interpret the result

Browser commit results use these dispositions:

- `not_performed`: Ricky deterministically stopped before dispatch, commonly because approval was
  denied or the reviewed browser binding became stale.
- `performed`: the local browser backend completed the approved activation.
- `in_doubt`: dispatch may have begun, but Ricky could not obtain reliable completion evidence.

`performed` is not proof that a payment settled, a form was accepted, a reservation exists, or a
message was delivered. Ricky returns the fresh browser-visible postcondition and ties it to the
approved envelope, but confirmation pages and order references remain untrusted site content.
Inspect the site or external account before retrying an `in_doubt` transaction. Ricky never
automatically replays it.

## Approve or deny in a terminal chat

Check every proposed field and the local binding before entering `y`. Press Enter, enter `n`, close
the prompt, or provide no interactive input to deny. Transaction approvals never offer a remembered
grant, and an installation allow rule cannot suppress the fresh prompt.

## Reconcile an uncertain background transaction

Use `/status` to inspect the linked execution and pending approval. If durable evidence says a
browser transaction is `in_doubt`, inspect the external site or account before attesting what you
found:

```text
/reconcile browser_transaction_<id> performed <operator note>
/reconcile browser_transaction_<id> not_performed <operator note>
```

Reconciliation records the authenticated operator, source message, note, disposition, and time.
It does not rewrite the original browser evidence or turn local browser completion into proof of
payment settlement, delivery, or remote acceptance.
