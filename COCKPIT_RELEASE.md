# Cashflow cockpit release

## Scope

The approved cockpit replaces the default dashboard screen. The existing case,
source, budget and forecast screens remain authenticated at `/dashboard/reference`.
No bank transfers or creditor sends are added. No production data is changed by
building this release.

## Included

- Responsive muted-green layout, prominent available-spending headline.
- Personal and three company views, separate balances and cash floors.
- One atomic balance update form; optimistic concurrency rejects stale submissions.
- Persistent monthly EUR 1,500 living allowance with signed carryover.
- Explicit starting remaining allowance, rather than inventing historic spending.
- Net balance-change reconciliation; no inferred payment or income matching.
- Today’s instalments, draft messages, recurring payments and expected receipts.
- Manual confirmation of a dated recurring occurrence without deleting its rule.
- Balance refresh required after confirmed payment/receipt before showing allowance.
- Next seven days, actionable clarification forms, separate reference archive.
- Existing authentication, CSRF, no-store and restrictive CSP retained; no JavaScript required.

## Conservative allowance

The allowance is capped by the remaining living budget and the dated 90-day
cashflow headroom. Current-month living allocation is added back once when
calculating spendable cash, while future months stay reserved. Unknown data,
unreconciled movements, stale balances and forecast shortfalls suppress the number.
No unconditional EUR 500 bank-balance guarantee is made.

This does not fix missing Gmail history, unresolved creditor balances or unknown
bill dates. Those remain visible blockers rather than invented promises. Net
balance changes cannot identify transactions, and mixed changes remain unresolved
until the user reconciles them. The implementation is intentionally conservative:
some unrelated source uncertainties may also pause the allowance.

## Deployment

New files required in the image: `cockpit.py` (included in both Dockerfiles).
Schema is additive and created idempotently. Existing source/case/payment tables
are retained. Budget and reconciliation data are stored on the existing SQLite
volume. No credentials or private source register are committed.

Promote the release commit to `main` using a non-force fast-forward, then deploy
that commit to the existing Railway service. Check the deployment commit and
successful Gmail sync log. Visit `/dashboard` and all four account groups.
No additional environment variables are required.

Before the first allowance can be calculated, initialize the remaining living
allowance and reconcile actual bank balances. Do not reapply old baseline balances
if newer balances have already been entered.

## Verification

Run `python -m unittest discover -q` and `python -m compileall -q .`.
The existing 107-case/17-instalment source snapshot was imported into an isolated
temporary database and all four cockpit views rendered successfully. Missing
information correctly produced no spending recommendation.

Local browser inspection was blocked by the browser environment
(`ERR_BLOCKED_BY_CLIENT` at localhost). Automated rendering/action tests passed,
but a real-browser visual check is still needed before calling visual QA complete.

For a local empty-data visual preview, run `python preview_cockpit.py` and open
`http://127.0.0.1:8765`. It creates a disposable database and cannot access production.
Its forms are display-only. Do not expose this preview server publicly.

## Rollback

Redeploy the preceding production commit. Additive cockpit tables can remain on
the persistent volume. Do not remove or reset the database.
