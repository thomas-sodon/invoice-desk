# InvoiceDesk web app (`apps/web`)

Next.js 15 (App Router), TypeScript strict, Tailwind 4, shadcn-style components, dnd-kit for the
board, pdf.js for PDF originals and a canvas layer for box overlays.

| Page | What it does | Spec |
|---|---|---|
| `/` | Board: drag-and-drop upload, **Run 25-invoice demo**, live Kanban (Received, Extracted, Validated, Ready to pay, Needs review, Flagged, Posted), live batch cost meter, **Post N to QuickBooks**. `?doc=<id>` opens the invoice panel; `?hero=1` renders the §9 hero layout | M1, M9, US-1, US-8 |
| `/documents/[id]` | Viewer: page image with green/amber/red boxes, revealed hidden/faint text boxed in red, plain-English checks, correction, approvals, tiers and cost, tamper-evident history | M5, US-2, US-3 |
| `/review` | Review queue: one-click correction, then the checks re-run | M10 |
| `/approvals` | Two-person approval with callback attestation; uploader and clerks never see approve | US-5 |
| `/followups` | Vendor email drafts: edit, approve and send | M15, US-7 |
| `/dashboard` | Owner dashboard in dollars and hours, labelled "benchmark on synthetic data" | M12, US-6 |
| `/compare` | Basic AI vs InvoiceDesk: red WRONG stamps vs green checks | M13, US-10 |
| `/try` | Try-it: Turnstile, redaction banner, limits, replay fallback when the daily spend cap is hit | M14, US-9 |
| `/scorecard` | Metrics with 95% CIs, reliability diagram, Report Card PDF link | M17, US-11, US-12 |

## Modes

- **Mock** (`NEXT_PUBLIC_API_MOCK=1`, or no `NEXT_PUBLIC_API_BASE`): everything runs in the browser
  on fixture data from the synthetic demo batch. `lib/mock/server.ts` simulates realtime card
  movement, corrections, approvals (uploader refused, two people, callback), posting, follow-ups
  and try-it. The UI shows "Offline demo" and labels sample numbers as samples.
- **API** (`NEXT_PUBLIC_API_BASE=http://localhost:8000`): `lib/http-client.ts` calls
  `/api/v1` per `docs/api-contract.md`; realtime uses SSE `GET /api/v1/events` read via `fetch`
  (so auth headers work) with reconnect. Auth is a Supabase JWT from `localStorage`
  (`invoicedesk.jwt`) or, in offline mode only, `X-Dev-User` from the "Signed in as" picker.

`lib/api-types.ts` mirrors the contract JSON exactly (`*_cents` integers, USD decimal strings,
`DocStatus` and column unions) with runtime guards for untrusted payloads. Money is formatted from
integer cents with BigInt arithmetic (`lib/money.ts`); bank and account numbers are masked to the
last 4 again in the UI (`lib/mask.ts`), and `redactions` paint opaque bars over printed numbers.

## Commands

```bash
pnpm --filter web dev          # http://localhost:3000 (mock mode by default)
pnpm --filter web typecheck
pnpm --filter web lint
pnpm --filter web test         # vitest
pnpm --filter web build
# screenshots + 375 px no-horizontal-scroll check against a running mock-mode app
NODE_PATH=/opt/node22/lib/node_modules BASE_URL=http://localhost:3000 node apps/web/scripts/screenshots.cjs
# rebuild fixtures from data/demo + data/gold/demo
uv run python apps/web/scripts/build_mock_fixtures.py
```

Screenshots land in `apps/web/screenshots/` (hero.png is 1000x750 at 2x).
