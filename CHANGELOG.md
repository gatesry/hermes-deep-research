# Changelog

## 0.1.0 — 2026-11-25

First release.

- `deep_research` tool with `start`, `status`, `result`, `resume`, `cancel` and `capabilities`.
- Portable engine: plan, search, fetch, extract, mechanical quote and arithmetic checks, separate model audit.
- Report sections: Findings, Disagreements, Unresolved, Sources; byte-range citations.
- `depth` presets (`quick`, `standard`, `deep`) over shared call budgets and one deadline.
- Background jobs in a per-profile SQLite store: idempotency keys, 32 jobs per conversation,
  7-day retention, heartbeat, cancel, and receipt-based resume that never repeats an interrupted call.
- Fetches only URLs supplied by the caller or returned by search, through Hermes' own `web_extract`.
- Domain filters normalise hosts (case, IDNA, trailing dot) and match on label boundaries.
- URLs are canonicalised (fragment, default port, tracking parameters, scheme and trailing-slash
  variants) before dedup, so duplicates are fetched and charged once.
- Fetch workers keep the caller's profile context; job admission, the per-conversation quota and
  idempotency checks are race-free; plugin unload stops running jobs; malformed model output becomes
  gaps instead of failing the job; fetching stops when the corpus is full; repeated receipts count
  once; document-only jobs skip planning; supplied documents never feed follow-up searches.
- Report footer states that verified is not proof of truth; no confidence scores.
