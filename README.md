# hermes-deep-research

A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin that adds a `deep_research` tool:
bounded, source-backed web research that runs as a background job and returns a report in which
every finding is backed by an exact quote from a source it actually fetched.

It is a Python port of the portable engine in
[OpenClaw's Deep Research extension](https://docs.openclaw.ai/tools/deep-research) (MIT). See
[Differences from OpenClaw](#differences-from-openclaw).

## What it does

1. **Plans** up to four web searches from your question (broad first, then narrow; primary sources preferred).
2. **Gathers** pages through Hermes' own `web_search` and `web_extract` tools, using whatever search
   and extraction backends you have configured.
3. **Extracts** atomic findings, each tied to verbatim quotes from the fetched text.
4. **Checks mechanically**: every quote must appear in its source (at least 20 non-space characters),
   and any derived number (sum, difference, ratio, percent change) must be recomputed from two quoted
   inputs with matching units and dates.
5. **Audits** the surviving findings in a separate model pass for semantic support, contradictions,
   units and dates.
6. **Repeats** up to three rounds while gaps remain and budget allows, then renders a Markdown report.

The report always has four sections: **Findings** (with `[S1]`-style citations and byte offsets),
**Disagreements** (conflicts between sources, or "None found"), **Unresolved** (gaps, failed checks,
exhausted budgets), and **Sources**, followed by a one-line note that "verified" is not proof of
truth. There are no confidence scores. Anything that could not be verified is listed, not dropped
silently. A job whose evidence has gaps finishes as `partial`, not `completed`.

## Install

```bash
hermes plugins install gatesry/hermes-deep-research --enable
```

The tool appears in your **next** session (tool lists are fixed when a session starts). It has no
Python dependencies beyond the standard library. You need working `web_search` and `web_extract`
backends in Hermes; check with `hermes tools`.

## Use

Ask Hermes for research and it will call the tool; or be explicit:

> Use deep_research (depth quick) to find how many people visited the Louvre in 2024, as of 2025-06.

The tool contract:

| Action | Arguments | Returns |
|---|---|---|
| `start` | `idempotencyKey`, `request` | Waits up to `foreground_wait_seconds`, then the job (and a compact result if finished) |
| `status` | `jobId` | State, progress, usage |
| `result` | `jobId` | Full result: report, citations, findings, sources with text, gaps, disagreements, usage |
| `resume` | `jobId` | Continues a paused job from its recorded receipts |
| `cancel` | `jobId` | Stops at the next call boundary |
| `capabilities` | — | Engines, budget defaults/maximums, depth presets, limits |

`request` fields: `question` (required, ≤8,000 chars), `output` (what to focus on), `asOf` (date the
answer should hold for), `depth` (`quick`, `standard`, `deep`), `budget` (overrides), and `sources`:
`web` (bool), `urls` (≤20 to fetch), `documents` (≤10 `{title, text}` you supply), `allowDomains` and
`denyDomains` (≤20 each). Supplying documents turns web search off unless you set `web: true`.

Pass a fully specified question. The tool does not ask clarifying questions; the calling agent is
expected to settle scope with you first.

### Budgets

Model calls, searches and fetches share one budget per job, and there is one deadline. Replayed
receipts count toward the budget too.

| Preset | Model calls | Searches | Fetches | Parallel fetches | Deadline |
|---|---|---|---|---|---|
| `quick` | 4 | 3 | 6 | 2 | 90 s |
| `standard` (default) | 8 | 6 | 12 | 2 | 180 s |
| `deep` | 20 | 14 | 30 | 4 | 600 s |
| maximum | 30 | 20 | 40 | 4 | 900 s |

Token counts are reported but are **not** a hard cap. A small `quick` job typically uses 3 model calls.

## Configuration

Settings live under `plugins.entries.deep-research.settings` in `config.yaml` (or the Desktop
Plugins tab):

| Key | Default | Meaning |
|---|---|---|
| `foreground_wait_seconds` | 300 | How long `start`/`resume` block before returning a running `jobId` (0–360). Hermes' generic tool timeout is 420 s, so keep this below it. |
| `max_active_jobs` | 4 | Jobs running at once in one Hermes process (1–16). |
| `max_output_tokens` | 4096 | Output cap per model call (512–32768). |

**Models.** Planning and extraction use your active main model through Hermes' plugin LLM API. The
audit uses the auxiliary task `deep_research_audit`, which follows the main model unless you set
`auxiliary.deep_research_audit` (for example, to a different model for a more independent check).

**Storage.** Jobs live in `<hermes home>/plugin-data/deep-research/jobs.db`, per profile. Jobs are
scoped to the conversation that started them, capped at 32 per conversation, and deleted 7 days
after their last update.

## Disable or remove

```bash
hermes plugins disable deep-research   # keeps the job database
hermes plugins remove deep-research    # removes the plugin; plugin-data/deep-research stays until you delete it
```

A tool that is already wired into a running gateway stays callable until the gateway restarts.

## Security model

Research reads untrusted web pages, so the plugin keeps the loop narrow:

- **No network client of its own.** Every fetch goes through Hermes' `web_extract`, so Hermes' URL
  safety checks (private and cloud-metadata addresses, redirect targets) and your website policy
  apply. A test fails if the plugin imports an HTTP or socket library.
- **Only caller- or search-supplied URLs are fetched.** URLs a model writes, or copies out of page
  text, are never fetched. URLs are canonicalised first (host normalised, fragment, default port and
  common tracking parameters such as `utm_*` removed; http/https and trailing-slash variants treated
  as one), so duplicates don't spend the fetch budget.
- **Model calls have no tools.** Request and source text is passed as one labelled JSON data block
  with instructions to treat it as data. This lowers, but does not remove, prompt-injection risk; the
  two rules above are the real controls.
- **Domain filters normalise hosts** (lower-case, IDNA, trailing dot) before matching on label
  boundaries, and recheck the final URL after redirects. They choose which evidence is accepted;
  they are **not a network firewall**.
- **The report is rendered by code**, from findings that passed the quote check and the audit.
  Source titles and model text are Markdown-escaped, and a citation must name a retained source.
- **Results are stored as JSON** in SQLite; nothing is unpickled. Hermes' truncation footer (which
  names a local cache path) is stripped from fetched text.

Quote matching and a model audit reduce unsupported claims. They do not prove that a source is
correct or that the research is complete.

## Differences from OpenClaw

- **Portable engine only.** OpenClaw can also call Google's managed Deep Research; this plugin
  refuses any engine other than `portable`.
- **Hermes lifecycle.** Jobs run in background threads inside the Hermes process. If Hermes
  restarts mid-job, the job shows as `paused` (no heartbeat for 60 s) and does **not** resume on its
  own; call `resume`. There is no automatic wake-up message when a job finishes; poll `status`.
- **Recovery.** As in OpenClaw, completed calls are replayed from receipts, and a call that started
  but has no receipt is never repeated: the job finishes as `indeterminate` with the gap listed.
  After the deadline, or after `cancel`, resume replays receipts only and makes no new calls.
- **Additions:** `depth` presets, a Disagreements section, a fetch-origin rule, IDNA host
  normalisation, URL canonicalisation before dedup, a render-time citation check, and a
  "verified is not true" footer.
- **Search snippets are never evidence**, as in OpenClaw; only fetched page text is.

## Development

```bash
python -m unittest discover -s tests -t .     # standard library only, no network
hermes plugins validate .
hermes plugins doctor . --ci
```

`smoke/queries.md` has twenty questions for a manual check after prompt or engine changes.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE) for OpenClaw's license.
