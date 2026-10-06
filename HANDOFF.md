# Handoff — Bursa Equity Pipeline

Snapshot as of 2026-10-03 (updated after working through this file's own
"smaller, bounded" next steps, then the REIT/bank taxonomy expansion — see
"Session update (2026-10-03)" and "REIT/bank taxonomy expansion" below).
See `README.md` for the original design intent; this file is "what's
actually built and working right now."

## Session update (2026-10-03)

Worked through the smaller, bounded next-steps items in order:

1. **Supermax stock-code migration** — its real Bursa code is `7106`; the
   watchlist had it split across two wrong codes, `5151` (0 docs) and
   `5283` (2 docs, 120 facts, confirmed real). Migrated company id 20
   (5283 → 7106, keeping its data and its better `ir_homepage_url` from the
   5151 row), deleted the empty 5151 duplicate. 98 companies now (was 99).
2. **Hong Leong Industries (3301) continuation-page bug — fixed.** Root
   cause: its balance sheet's continuation page (liabilities + the "Total
   equity and liabilities" end line) ran `detect_columns` independently of
   the primary page (assets) - a wrapped label fragment ("Deferred tax"
   wrapping onto "liabilities" on the next line) landed just past the
   continuation page's own first-column boundary, creating an extra,
   spurious band there the primary page never had, shifting every real
   column one index to the right. New `page_scoring._remap_to_primary_columns`
   re-buckets a continuation page's cells into the *primary* page's own
   column bands by x-coordinate instead of trusting the continuation page's
   independently-detected numbering; a cell that falls outside every real
   band is dropped, never guessed into the nearest one. **Live-verified**:
   re-normalized/validated this one company, 12/12 rules now pass (was 2
   failing); full `bursa validate facts` confirms a clean -2 (78 → 76), no
   other company's count moved. 2 new unit tests in `test_page_scoring.py`
   exercise the remap function directly.

3. **Stale `Fact`/`ValidationResult`/`ExtractionRun` gap — fixed properly.**
   `normalize.py`'s fact-writing was a pure upsert keyed by the *current*
   extraction's own `(concept_key, period, basis, document_id)` - nothing
   ever deleted a `Fact` whose combination the current extraction stopped
   producing, and every `normalize facts` call created a brand-new
   `ExtractionRun` per document regardless, immediately orphaning the
   previous one (and anything downstream - `ValidationResult` rows written
   against it by an earlier `validate facts` run - with it). Fixed by
   tracking every fact id touched while writing one document's facts this
   run, then deleting whatever existing fact for that document *wasn't*
   touched, then deleting every other `ExtractionRun` row for that document
   (safe now that nothing references them - `ondelete=CASCADE` takes their
   `ValidationResult`/`RawRow` rows with them for free). New
   `FactWriteResult.facts_deleted`/`stale_runs_deleted` fields, surfaced in
   `bursa normalize facts`'s own output (a "Deleted" column, and a
   restated summary line). 2 new tests in `test_pipeline_normalize.py`
   (one re-extracts a document with deliberately different results and
   confirms the old periods' facts and the old run - plus a validation
   result manually attached to it - are gone; one confirms an unchanged
   re-extraction loses zero facts even though a fresh run still gets
   created and the previous one still gets cleaned up).

   **Live-verified**: the real DB already had exactly one document
   (Hong Leong Industries, from re-normalizing it earlier this session
   under the pre-fix code) carrying 2 `ExtractionRun` rows instead of 1;
   re-running `bursa normalize facts --company 3301` with the fix in place
   reported "2 stale extraction run(s) cleaned up", and `ExtractionRun` is
   now a clean 1:1 with documents (280/280) across the whole DB. Facts/
   validation counts unchanged (1016/940/76) - this only fixes bookkeeping
   that had already mostly been reset by an earlier full wipe-and-rebuild
   this session, not live data.

All three fixes are narrow and self-contained - no pipeline-wide
re-extraction needed for any of them (Supermax is a one-time DB edit; the
other two only needed `bursa normalize facts --company 3301` + a full
`bursa validate facts` to confirm, not a 2-hour full re-extraction). 255
tests pass (251 → 255). Cache regenerated (`cache/bursa_snapshot.pkl`).

All three "smaller, bounded" next-steps items are now done.

## REIT/bank taxonomy expansion (same day, 2026-10-03)

The deferred "REIT rental income as IS first line" request, generalized:
rather than guess at vocabulary, read the *actual* unmapped rows from real
bank/REIT filings already ingested (RHB Bank, Alliance Bank, AFFIN Bank,
Hong Leong Bank, Kenanga Investment Bank, Public Bank; IGB REIT, Pavilion
REIT) - 765 distinct unmapped `(statement, label)` combos across the banks
alone, 94 for the two REITs. Added **33 new concepts and 102 new synonyms**
to `taxonomy.py` from what that actually showed:

- **REIT-specific**: `is.net_property_income`, `is.property_operating_expenses`,
  `is.manager_fees`, `is.trustee_fees`, `is.distributable_income`, `is.dpu`,
  `bs.units_in_circulation`, `cf.payment_for_investment_properties` - plus
  synonym additions on *existing* concepts for REIT-specific wording of the
  same role ("rental income"/"lease revenue" → `is.revenue` - the originally
  requested fix; "unitholders' capital" → `bs.share_capital`; "accumulated
  income" → `bs.retained_earnings`; "total unitholders' fund" →
  `bs.total_equity`; "distribution to unitholders" → `cf.dividends_paid`;
  "basic/diluted earnings per unit" → `is.eps_basic`/`is.eps_diluted`; etc).
- **Bank-specific**: `is.net_interest_income`, `is.fee_commission_income`/
  `_expense`, `is.islamic_banking_income`, `is.operating_profit_before/
  after_allowances`, `is.allowance_credit_losses`, `bs.customer_deposits`,
  `bs.loans_advances_financing`, `bs.subordinated_obligations`,
  `bs.repo_obligations`, `bs.bills_acceptances_payable`,
  `bs.statutory_deposits`, `bs.financial_investments_amortised_cost`,
  `bs.provision_zakat` - plus synonyms on existing concepts for Islamic-
  finance/zakat phrasing of the same PBT/tax role.
- **General** (found via this lens, not actually industry-specific):
  `bs.derivative_assets`/`liabilities`, `bs.borrowings_total` (an unsplit
  "Borrowings" line), `bs.other_assets`/`other_liabilities`,
  `bs.pledged_deposits`, `cf.interest_received`, `cf.changes_in_inventories`/
  `receivables`/`payables` (breaking `cf.working_capital_changes` into the
  per-line detail some issuers show instead of one combined figure).
- **Deliberately skipped as too ambiguous to seed globally**: "Net income"
  (a bank's own operating-income subtotal, 22 occurrences) and "Investment
  income"/"Other assets"/"Other liabilities" in the generic sense were
  considered but "Net income" specifically was left unmapped rather than
  risk colliding with "net profit" if a non-bank's row happened to say the
  same two words - synonym matching here is global text, not industry-
  scoped (no `Company.industry`/`sector` field exists to scope it by).

**A real, general (not industry-specific) normalization bug found the same
way**: `normalize_label` only stripped a parenthesised alternative in
*slash-then-paren* order ("net cash from/(used in)..."), never the reverse
*paren-then-slash* order ("net cash (used in)/from...") - confirmed real on
a REIT's own cash flow statement, silently failing to match an otherwise-
correctly-seeded synonym. Fixed with a second regex
(`_PAREN_SLASH`); affects any issuer phrasing it either way, not just REITs.

**Live-verified, not just synthetic lookups**: re-seeded (`bursa seed`:
+33 concepts, +102 synonyms) and re-normalized the 8 companies above -
581 new facts written, 785 updated. `bursa validate facts` on just those 8:
**59 rules run, 58 passed, 1 failed** (RHB Bank, `is_pbt_to_pat`, FY2020
only - PBT minus tax is off by RM34.66m against the recorded profit for
the period; every other year for RHB and every other company here is
clean - not yet diagnosed, a new, narrow finding from richer coverage, not
a regression). Full-DB `bursa validate facts`: **1016/940/76 → 1031/954/77**
(+15 more rules now checkable at all, +14 more passing, the 1 new RHB
failure). 258 tests pass (255 → 258: 3 new - the paren/slash fix, and two
lookup tests covering a representative sample of the new bank/REIT
concepts). Cache regenerated.

## Session update (2026-10-02, later the same day)

Triaged the 105 validation failures (this file's own #1 suggested next
step). Two real bugs found and fixed:

1. **A 1000x scale bug** (`extract/layout.py`): a unit/year header row
   (`"(RM'000) 2022 2021 (%)"`) was misclassified as the first data row
   because "2022"/"2021" parse as plain numbers, cutting off the real header
   text before the scale token ever reached `detect_scale()` — every
   quarterly-report figure shaped like this was stored at 1/1000th its real
   value. Fixed via a `_BARE_YEAR` exclusion in `_numeric_hits`.
2. **Quarterly reports written as fake 12-month "FY" periods**
   (`pipeline/normalize.py`): `period_type` was hardcoded to `FY` for every
   non-instant statement — no quarter/half-year detection existed at all.
   Fixed by reading the statement's own duration caption ("Three/Nine Months
   Ended") via two new `bursa.normalize.periods` functions
   (`parse_statement_duration_months`, `resolve_duration_period_type`); an
   interim statement found before the company's real fiscal year end is
   known is skipped, never guessed.

Both re-verified against the live data (not just the 239-test suite, up
from 236 — 3 new regressions added, one of them via a new synthetic fixture
`QUARTERLY_INCOME_STATEMENT_Q1_ONLY`): re-ran `extract → normalize →
validate` after each fix (~2–3hrs each, 291 docs). The second fix also
surfaced a real instance of the already-documented "stale fact" gap
(old wrongly-FY-typed facts sat alongside new correctly-typed ones after
re-normalizing) — worked around this round by wiping the fully-derived
`Fact`/`Period`/`ExtractionRun`/`ValidationResult` tables and rebuilding
from the (untouched) source PDFs; the underlying gap itself is still open,
see below.

**Result: 970/865/105 → 78 failures now** (938/1035 and 936/1014 were two
intermediate readings mid-fix — rerun `bursa validate facts` for the live
number). United Plantations (5101), the scale bug's entire blast radius,
now has **zero** failures. SP Setia (3704) dropped 22 → 13 (partially the
same quarterly-period cause, since it also files quarterly reports) but
13 failures remain with a different shape (large, irregular
`is_pbt_to_pat`/`is_pat_split` deltas, >100% revenue swings) — not yet
diagnosed.

Also built `scripts/export_cache.py` → `cache/bursa_snapshot.pkl`: a pickled
dump of every fact + validation result, so inspecting the data doesn't
require re-running the 2+ hour pipeline. Gitignored; regenerate after any
future `normalize facts`/`validate facts` run (just done — current).

## Pipeline stage status

| Stage | Status |
|---|---|
| `ingest` | Working. Drop-folder + scraper, sha256 dedupe. |
| `extract` (page selection + layout) | Working. Deterministic, no LLM. Long accuracy-hardening history — see memory. |
| `normalize` (facts) | Working. Deterministic, no LLM/API key needed. `bursa normalize facts`. |
| `validate` (accounting identities) | Working. `bursa validate facts`. |
| LLM mapping (`llm_mapper.py`) | **Built, never run** — no `ANTHROPIC_API_KEY` in this environment. Synonym table covers most real rows without it. |
| Scraper (IR sites) | Working, multi-year (`--years N`). bursamalaysia.com itself is permanently blocked (Cloudflare). |
| Publish / web UI | **Not built.** The "Statement Extraction Ledger" artifact is the only visual surface — see link below. |

The README's original plan was LLM-driven concept/period mapping with deterministic
extraction underneath. What actually got built and is carrying the project is the
reverse emphasis: a fully deterministic `extract → normalize → validate` path that
needs no API key at all. The LLM path is real code, just unexercised here.

## Current data snapshot (live counts)

- **98** watchlist companies, all with an `ir_homepage_url` set (down from 99
  — Supermax's zero-doc duplicate stock code removed, see "Session update
  (2026-10-03)").
- **291** ingested documents across **74** companies (up from 96 docs / ~40 companies
  before this session's multi-year backfill).
- **12,818** facts in the `facts` table, across **62** companies (up from
  12,260 after the REIT/bank taxonomy expansion — see "Session update").
- Latest `bursa validate facts` run: **1031 rules run, 954 passed, 77 failed.**
  (The raw `validation_results` table currently shows more rows than that — see
  "Known issues" below; trust the CLI's own printed count from a fresh run, not
  a raw `SELECT count(*)`.)
- A pickled snapshot of all facts + validation results is kept at
  `cache/bursa_snapshot.pkl` (regenerate with `uv run python
  scripts/export_cache.py`) so this data can be inspected without re-running
  the ~2hr `extract → normalize → validate` pipeline.

## Known open issues

- ~~**Hong Leong Industries (3301) continuation-page bug**~~ — **fixed
  2026-10-03**, see "Session update" above.
- **77 current validation failures, partially triaged.** Five rounds of
  failures have now each been traced to real extraction bugs and fixed (a
  Note-column mix-up, a duplicate owners/NCI row collision, the RM'000
  bare-year scale bug, the quarterly-as-fake-FY bug, Hong Leong Industries'
  continuation-page shift — all above) — worth assuming more of these 77
  are real bugs, not noise, until checked. Biggest remaining cluster: SP
  Setia (3704, 13 failures) and 7-Eleven Malaysia (5250, 12) — large,
  irregular `is_pbt_to_pat`/`is_pat_split` deltas and >100% revenue swings
  too big/odd to be the bugs already fixed. **New, narrower finding from
  the taxonomy expansion**: RHB Bank's FY2020 `is_pbt_to_pat` is off by
  RM34.66m (profit before tax minus tax expense doesn't equal profit for
  the period) - every other year for RHB, and every other bank/REIT newly
  covered this round, is clean, so this isn't systemic. Next place to
  apply the same triage pattern that already found 5 real bugs.
- ~~**`Fact`/`ValidationResult`/`ExtractionRun` rows go stale on
  re-extraction**~~ — **fixed 2026-10-03**, see "Session update" above.
  `normalize facts` now deletes a document's own facts the current
  extraction no longer produces, and every other `ExtractionRun` row for
  that document, each pass - no more wipe-and-rebuild needed to trust
  validation numbers after an `extract`/`normalize` code change.
- ~~**AMMB and Public Bank only ever yield 1 year** from `--years 5`~~ —
  **both fixed and verified live 2026-10-03.** First fix attempt (dynamic
  hop-limit escalation, same day) turned out to be solving the wrong
  problem - checked directly against both live sites and found neither's
  "current year" page links to *anything* else at all in static HTML (no
  nav hop, however deep, would ever find an older year). The hop-escalation
  code is still real and kept (a genuine, separately tested improvement for
  a future site that actually *is* hop-depth-limited - configurable via
  `scraper_base_hops`/`scraper_hop_escalation_step`/
  `scraper_max_hops_ceiling`/`scraper_max_pages_per_sniff` in `.env`), it
  just wasn't what was blocking these two.

  **Public Bank, user-reported and confirmed live**: its page carries a
  fully server-rendered `<select name="year">` dropdown naming every year
  back to 2004, each `<option>`'s own `value` a real, independently-
  fetchable static page - nothing before this looked at `<option>` tags at
  all, only `<a href>`. **Live-verified**: `--company 1295 --years 5
  --dry-run` now finds 5 distinct years (2022-2026) where it found 2 before.

  **AMMB, same underlying pattern, a different widget**: its page uses a
  sliding year-tab bar instead of a dropdown - also just ordinary `<a
  href="/investor-relations/annual-report/fy2025">FY2025</a>` links the
  whole time, missed only because "FY2025" matches none of `NAV_PATTERN`'s
  phrasings, plus an "Archive" tab bundling 2004-2011 in one page. Both
  shapes now share one function, `find_year_nav_candidates`. Getting this
  one genuinely clean took two more rounds past the first working version:
  AMMB's own hub page turned out to *also* embed a sitewide "related year"
  footer duplicating every year across unrelated sections (financial
  results, an investor calendar, a company-awards page) - fixed by
  requiring a year candidate's own target URL (not just its label, not just
  the page it's found on) to itself look like an annual-report page.
  **Live-verified**: `--company 1015 --years 5 --dry-run` now finds 5
  distinct years from 1 before.

  **The one gap found during this verification (a same-year decoy
  satisfying "enough" before the real page for that year was ever visited)
  is now also closed, same day, on request ("content check before confirm
  all collected")**: `sniff_annual_report` gained an opt-in
  `verify_content` parameter - every candidate is downloaded and content-
  checked (`content_filter.has_financial_statements`) *during the crawl
  itself*, before its year is allowed to count at all, not only afterwards
  per-candidate during ingest. `scrape_company` turns this on whenever a
  scrape isn't a dry run (a dry run keeps its existing never-downloads
  contract). **Live-verified**: the exact AMMB gap above is gone - sniffing
  directly against the live site now returns all 5 real years
  (2022-2026), each from the genuine `/annual-report/fy{year}/` path, zero
  decoys, in 8 page fetches (down from the escalation-driven chase before,
  since the crawl now correctly stops the moment 5 *verified* years are
  found rather than wasting hops on unrelated pages at all). Public Bank
  re-confirmed 5/5 afterwards, no regression.

  Real tradeoff, not free: this downloads every candidate found during
  sniffing, including ones that don't end up kept, and a kept candidate
  gets downloaded a second time later by the ingest pass (`sniff_annual_report`
  has no way to hand its own download across that boundary yet) - acceptable
  since the redundant half is bounded by how many years are actually kept
  (typically small), not by how many candidates exist on a page.
- **`EXCLUDE_PATTERN` (scraper decoy filter) is very likely still incomplete.**
  Three separate rounds of testing against live sites each found one or two
  *more* real decoy brandings (investor conference/roadshow decks, "Invest
  Malaysia", "Corporate Day", etc.) than the last. Expect to find more when
  scraping sites or years not yet tested.
- **~1.5% of facts** (long-tail from earlier in the session) still look suspect
  from rare garbled-header column layouts the current heuristics don't catch.
  Flagged, deliberately not chased further per an earlier explicit decision.

## CLI cheat sheet (dependency order)

```powershell
uv run bursa seed                                    # taxonomy + synonyms
uv run bursa ingest                                  # drop-folder PDFs
uv run bursa scrape annual-reports --years 5          # IR site backfill (--company CODE to scope)
uv run bursa extract statements                       # writes statement_extraction.json
uv run bursa normalize facts                          # writes Period/Fact rows
uv run bursa validate facts                           # writes ValidationResult rows, prints failures
uv run bursa scrape prune --apply                     # delete decoy documents that slipped through
uv run pytest -q                                       # 258 tests
```

## Artifact

"Statement Extraction Ledger" — per-company raw extraction + facts view, multi-year:
https://claude.ai/code/artifact/528e140e-3705-4e1b-804e-891440b922c0

## Suggested next steps

1. ~~REIT/bank taxonomy gaps~~ — **done 2026-10-03**, see "REIT/bank
   taxonomy expansion" above. 33 new concepts, 102 new synonyms,
   live-verified against 8 real bank/REIT companies.
2. Keep triaging the remaining 77 validation failures — start with SP Setia
   (3704, 13 failures) and 7-Eleven Malaysia (5250, 12), the two biggest
   clusters, or the new narrower RHB Bank FY2020 `is_pbt_to_pat` finding -
   same pattern that already found 5 real bugs this way.
3. Run the backfill for the remaining years/companies not yet covered, and keep
   extending `EXCLUDE_PATTERN` as new decoy classes turn up.
4. Wire `llm_mapper.py` in once an API key is available, for rows the synonym
   table still can't resolve.
5. The deliberately-skipped ambiguous bank terms ("Net income" as a bank's
   own operating-income subtotal, etc.) would need either a company-scoped
   synonym per issuer or a `Company.industry`/`sector` field to seed
   globally without risking a false match on a non-bank's row that happens
   to say the same words.

## Where the detailed history lives

This file is the snapshot. The incident-by-incident "why" — every bug found,
how it was diagnosed, what was tried and rejected — lives in Claude's project
memory, not here:

- `bursa-pipeline-scope.md` — extraction/layout/scraper accuracy work, the long
  first half of this project's history.
- `bursa-facts-pipeline.md` — the `normalize`/`validate` stages, bugs found
  wiring them up.
- `bursa-multiyear-backfill.md` — the `--years` scraper work and the
  `write_facts_for_company` multi-year bug.
