# Pinpoint Partner Intelligence

Pinpoint helps a legal recruiter prepare for a call by finding a corporate or M&A partner, checking their office and official profile, and seeing what changed between accountable roster observations.

The bundled roster was sourced on **19 November 2026**. It covers **14 of the 15 requested firms**, including an officially confirmed empty scope at Quinn Emanuel, and contains **2,920 partner records**. The Firms view gives the source, verification date, and coverage state. The synthetic demo runs separately.

## Run the assessment

Use Python **3.10 or later** for the offline walkthrough and dashboard from the root of a source checkout on macOS or Linux. Use a current **Python 3.14 or later** release for live refreshes and network diagnostics, which check that the interpreter supports wildcard and longest-match rules for robots.txt. The application has no third-party Python dependencies. Port 8000 is the default. [Python robots parser change](https://github.com/python/cpython/pull/149374)

### 1. Open the source-backed roster offline

~~~sh
python3 -m pinpoint import-roster rosters/baseline.json
python3 -m pinpoint serve --build
~~~

Open [the local dashboard](http://127.0.0.1:8000/). The **Partners** tab opens on real data. Search by name or title, filter by firm, practice, or office, then open a partner to see their profile link and the firm's last verification. The **Firms** tab shows verified firms and coverage gaps. The baseline establishes the initial roster, so its first observation produces no inferred arrivals.

Stop the server with Ctrl-C. Use `--port 8001` if port 8000 is occupied. Add `--db /path/to/roster.db` to both commands to use a different database. `serve --build` writes the dashboard and exports to `out/`.

### 2. Walk through roster changes offline

~~~sh
python3 -m pinpoint demo
python3 -m pinpoint serve --demo --port 8001
~~~

Open [the synthetic demo](http://127.0.0.1:8001/). The demo loads two dated sample observations with changes in partner membership, title, office, practice, and contact. Sample names, links, and contact fields are synthetic. The demo writes to `data/demo/` and `out/demo/`. Running it again reuses the initialized example.

### 3. Refresh verified sources

~~~sh
python3 -m pinpoint sync --firm cravath --verbose
python3 -m pinpoint sync --live --json
~~~

The first command checks one firm. The second attempts all configured firms and returns status **1** if any firm could not be verified. Successful firm updates and the failure outcomes publish together. Failed firms retain their last verified rows, if available, and their verification dates. Paul Hastings and the reviewed-only Wachtell source currently make the all-firm command return **1**. The initial Paul Weiss profile crawl took about 25 minutes at the configured per-host request interval.

Set `PINPOINT_USER_AGENT` before scheduling if operating under a different contact. Use a descriptive crawler name and a contact URL or monitored address that you control.

### 4. Check and inspect the project

~~~sh
python3 -m compileall -q pinpoint tools
python3 -m unittest discover -v
python3 -m pinpoint doctor
python3 -m pinpoint search --firm cravath --office "London"
python3 -m pinpoint snapshots
python3 -m pinpoint history --firm cravath
python3 -m pinpoint report
~~~

| Command | Output or behavior |
|---|---|
| `init` | Registers all configured firms. Safe to rerun. |
| `firms` | Lists firm slugs and configured sources. |
| `import-roster MANIFEST` | Validates and publishes a dated official-source manifest. |
| `sync [--live] [--firm SLUG] [--verbose] [--json]` | Refreshes official sources. Repeat `--firm` to select several. |
| `sync --fixture-set current` | Loads synthetic fixtures into the demo database. `next` loads the changed set. |
| `serve [--demo] [--build] [--port N]` | Serves the dashboard and read-only API on loopback. |
| `report [--db PATH] [--out DIR]` | Writes the dashboard shell, active roster CSV, latest 500 changes CSV, and JSON export. |
| `search`, `changes`, `snapshots`, `history` | Queries the current roster and its history. Use `--help` for options. |
| `doctor [--check-network]` | Reports dependency and configuration information. The optional network check samples robots availability. |

Run `python3 -m pinpoint COMMAND --help` for command-specific options. Run from source for the assessment so the config, fixtures, and bundled baseline are available.

## Coverage and scope

The app counts active partners using each firm's official corporate or M&A practice filters or published profile labels. It includes the offices published for each person. A multi-office partner counts once in the roster and once in each listed office. Adjacent practice labels qualify where the firm's taxonomy explicitly groups them with corporate or M&A. Paul Hastings remains unresolved and its roster count is unknown. The baseline records three profiles whose official sources leave the office blank.

| Firm | Roster | Evidence on 19 November 2026 |
|---|---:|---|
| Kirkland & Ellis | 632 | Public practice and partner filters reconciled for [M&A](https://www.kirkland.com/services/practices/transactional/mergers--acquisitions), Corporate Governance, and Private Equity. |
| Wachtell, Lipton, Rosen & Katz | 35 | Reviewed all 291 entries in the official [View All attorney directory](https://www.wlrk.com/attorneys/?asf_l=View+All) and matched 34 Corporate partners and one Real Estate M&A partner. The firm lists [New York](https://www.wlrk.com/contact/) as its office. Direct crawl requests return HTTP 403, so this roster refreshes through reviewed imports. |
| Quinn Emanuel Urquhart & Sullivan | 0 | The firm explicitly describes its [M&A litigation practice](https://www.quinnemanuel.com/practice-areas/mergers-acquisitions-litigation/) and states that it has no corporate transactional practice. |
| Davis Polk & Wardwell | 189 | The public [lawyer directory](https://www.davispolk.com/lawyers) returned 211 partner-filtered entries across all offices. The roster uses the published current title and the firm's [Corporate practice taxonomy](https://www.davispolk.com/corporate), which includes M&A. |
| Simpson Thacher & Bartlett | 424 | The public staff directory reconciled the Corporate and [M&A](https://www.stblaw.com/client-services/practices/corporate/mergers-and-acquisitions/corporate-m-a) taxonomy and partner-role filters. |
| Paul, Weiss, Rifkind, Wharton & Garrison | 228 | Verified all 551 partner-and-counsel profiles in the [public sitemap](https://www.paulweiss.com/sitemap.xml) and cross-checked membership against the [Corporate](https://www.paulweiss.com/practices/transactional/corporate) and [M&A](https://www.paulweiss.com/practices/transactional/mergers-acquisitions) practice pages. A [firm announcement](https://www.paulweiss.com/insights/firm-news/paul-weiss-adds-executive-compensation-partner-in-new-york) supplied one office omitted from a biography. The sitemap and profiles are permitted by robots.txt. |
| Gibson Dunn | 263 | The public directory reconciled the [M&A](https://www.gibsondunn.com/practice/mergers-and-acquisitions/), Private Equity, and Securities Regulation and Corporate Governance partner results. |
| Latham & Watkins | 476 | The official people search reconciled [M&A](https://www.lw.com/en/practices/mergers-and-acquisitions), Private Equity, Corporate Governance, Public Company Representation, Emerging Companies & Growth, and Shareholder Activism & Takeover Defense. |
| Cravath, Swaine & Moore | 65 | The public directory reconciled both the [Corporate](https://www.cravath.com/practices/practices/corporate/index.html) and M&A partner groups. |
| Milbank | 83 | The [Corporate](https://www.milbank.com/en/practices/areas/corporate/index.html) directory and three M&A views reached their terminal pages and reconciled unique profiles. |
| Sullivan & Cromwell | 160 | The public [General Practice](https://www.sullcrom.com/Practices/General-Practice/) (Corporate), M&A, and Financial Services M&A directory returned reconciled active-partner batches. |
| Paul Hastings | — | The crawler received HTTP 429 for robots.txt and stopped before requesting the [M&A practice](https://www.paulhastings.com/practices/mergers-and-acquisitions) or directory. |
| Skadden, Arps, Slate, Meagher & Flom | 152 | The public [Corporate](https://www.skadden.com/capabilities/practices/corporate) and M&A filters reconciled. One official record has an unpublished office. |
| King & Spalding | 117 | Official search results reconciled the [M&A](https://www.kslaw.com/capabilities/mergers-and-acquisitions), Private Equity, and Public Companies Advisory filters. |
| Weil, Gotshal & Manges | 96 | The official directory reconciled M&A, Private Equity, and Governance, Securities & Reporting for the [Corporate department](https://www.weil.com/experience/departments/corporate). |

[The baseline manifest](rosters/baseline.json) records each included firm's scope, observation time, official source, people, office counts, and count of people with unpublished offices. The baseline is a dated observation. Live refreshes and reviewed imports provide subsequent observations.

## Architecture and decisions

~~~text
Official directories / practice statement       Reviewed official-source file
                   |                                         |
          robots-aware HTTP                            schema + counts
                   |                                         |
       source adapters + reconciliation  --------------------+
                   |
       normalize, resolve identity, compare
                   |
       SQLite publication transaction
       active roster + snapshot + events + firm outcomes
                   |
       read-only local API, dashboard, CLI, exports
~~~

- **SQLite** gives the roster and its history a single atomic publication boundary and keeps the reviewer setup lightweight. Network calls finish before the write transaction. A failed adapter stages a firm health outcome, while an unexpected publication error rolls back the whole transaction. Fifty firms fit this model. A separate database service would add operations to a small, single-writer system.
- **Official practice metadata** makes membership explainable. The adapters validate reported totals, page boundaries, source IDs, practice markers, or filter confirmations where available. Sitemap discovery follows declared same-host child maps and deduplicates profile URLs. Paul Weiss checks every listed partner-and-counsel biography and reads its labeled office and practice links. An incomplete crawl preserves the last verified roster.
- **Dated snapshots** support historical queries and make the change feed repeatable. Every completed snapshot records the entire published roster, even on a targeted refresh. Firm freshness is tracked per source attempt so refreshing one firm cannot clear a different firm's failure.
- **Identity within a firm** uses a source ID when available, then unique verified profile and email aliases and unambiguous normalized names. Persisted keys survive normal churn and the departure of a namesake. Cross-firm person matching remains a follow-up requiring corroboration.
- **Roster observations** distinguish newly observed membership and removed membership from a confirmed employer move. The change log records title, office, practice, profile, and available contact changes. A first real observation or a switch in source establishes a baseline.
- **Standard-library stack** keeps data models, ingestion, SQLite, the CLI, and the server inspectable and portable. The dashboard uses HTML, CSS, and JavaScript with no build step. The bundled local server is intended for reviewer use on loopback. Shared team deployment needs an authenticated production service and TLS.
- **Minimal professional data** supports the workflow with name, firm, public title, practice, office, and official profile. The source-backed directory adapters avoid storing phone and email by default. Direct crawler requests use an identifiable user agent, per-host spacing, bounded response sizes and retries, and a fresh or explicitly revalidated successful response. An unreachable robots policy stops the crawl. Exports constrain profile URLs and neutralize spreadsheet formula prefixes.

## Onboarding, weekly operation, and recovery

1. Add or update the firm in `config/firms.json`. Verify the official practice scope, partner-role filters, global office behavior, stable identifier, and source pagination. Use `coverage_gap` while testing so incomplete output remains out of the verified roster.
2. Validate with a targeted run against a disposable database. Compare source totals, sampled official profiles, and office counts, then remove the gap after establishing the correct source. Add a deterministic fixture when a new response shape needs a regression check.
3. Use `import-roster` for an approved offline observation. Match the format in the bundled manifest and include the official source, timezone-aware verification date, review notes, all-office attestation, unique roster count, and office counts. Use zero rows only when the official source supports an empty scope. The importer rejects older evidence when a newer verified observation already exists.
4. Schedule `python3 -m pinpoint sync --live --json` weekly from the checkout. Capture stdout as the run result and alert on nonzero exit. Run `report` as a separate command after evaluating that status. The dashboard queries SQLite directly and continues to show the last published records.
5. Back up SQLite with its backup API while the system is running. For a manual filesystem restore, stop the writer and local server, preserve the affected database and sidecars, and restore a checked backup before restarting. Treat `data/cache/` as disposable and exclude it from backups and the submission. The cache can contain full public HTML and should be stored privately and pruned periodically.

For 50 firms I would keep the same adapter and publication model, give each source its own durable crawl checkpoint and request budget, and add limited concurrency across hosts. The first product priority is getting a reconcilable source for Paul Hastings, followed by automating Wachtell's reviewed refresh. Saved searches, automatic lateral matching, contact enrichment, and an authenticated team rollout come after that.

## Time spent

Approximately four and a half hours including the additional source verification.
