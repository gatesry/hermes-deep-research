# Manual smoke set

Twenty questions for a hand-run check after changes to prompts or the engine. Run a few with
`depth: "quick"` and read the report: are findings quoted from primary sources, are conflicts
listed under Disagreements, and are gaps honest? This is not run in CI and has no scoring.

| # | Question | What a good report leans on |
|---|----------|------------------------------|
| 1 | What is the current stable Python release and when was it released? | python.org release pages |
| 2 | What changed in the latest SQLite release? | sqlite.org release log |
| 3 | What is the population of Canada according to the most recent official estimate? | Statistics Canada |
| 4 | By what percentage did global average CO2 concentration change between 2015 and 2025? | NOAA / Mauna Loa data; both figures cited, change listed under Unresolved (inputs have different dates, so no checked calculation) |
| 5 | What are the licence terms of the Linux kernel? | kernel.org COPYING |
| 6 | Which HTTP status code means "Too Many Requests", and which RFC defines it? | RFC 6585 |
| 7 | What is the boiling point of water at the summit of Mount Everest? | physics references; note assumptions |
| 8 | What did the most recent IPCC synthesis report conclude about warming to date? | ipcc.ch |
| 9 | What is the current recommended adult daily vitamin D intake in the US? | NIH Office of Dietary Supplements |
| 10 | Is coffee consumption associated with lower or higher all-cause mortality? | systematic reviews; Disagreements expected |
| 11 | What were Apple's total net sales in its most recent fiscal year? | Form 10-K |
| 12 | In the latest BLS release, how many percentage points apart are the US unemployment rates for men and women aged 20+? | BLS; checked difference (same release date) |
| 13 | What is the TLS 1.3 handshake round-trip count compared with TLS 1.2? | RFC 8446 |
| 14 | When does the next total solar eclipse visible from Europe occur? | NASA eclipse pages |
| 15 | What are the main differences between the GPLv2 and GPLv3 licences? | gnu.org |
| 16 | Do minimum-wage increases reduce employment? | economics literature; Disagreements expected |
| 17 | What is the maximum size of a single SQLite database? | sqlite.org limits page |
| 18 | Which programming language topped the latest Stack Overflow developer survey for "most admired"? | survey site |
| 19 | What is the half-life of carbon-14? | NIST or standard references |
| 20 | What does the EU AI Act require of general-purpose AI model providers? | EUR-Lex text |
