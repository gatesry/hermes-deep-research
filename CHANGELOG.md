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
- URLs are fetched as given, minus equivalent spellings (host case/IDNA, default port, fragment);
  dedup also ignores common tracking parameters, so one page is fetched and charged once.
- Fetch workers keep the caller's profile context. Job admission, the per-conversation quota and
  idempotency checks are serialised (tested under concurrent starts). Plugin unload pauses running
  jobs. Malformed model output becomes gaps instead of failing the job. Fetching stops when the
  corpus is full; repeated receipts count once; document-only jobs skip planning; supplied
  documents never feed follow-up searches.
- `cancel` returns within about 5 s; finished jobs drop their resume receipts; dropped supplied
  URLs, URLs skipped at the corpus limit and a failing extract backend are reported as gaps; one
  refused page no longer stops other fetches; follow-up prompts list earlier queries.
- CI actions pinned to commit SHAs with a 10-minute timeout.
- Report footer states that verified is not proof of truth; no confidence scores.
