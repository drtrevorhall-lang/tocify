# tocify

Pulls new papers from journal RSS feeds and PubMed every Monday, ranks them against your
research interests with an (open) LLM, and publishes the result to GitHub Pages.

Live digest: <https://drtrevorhall-lang.github.io/tocify/>

## Quick start

Five minutes, assuming you forked this repo.

1. **Get an API key.** Sign up at <https://openrouter.ai>, then create a key at
   <https://openrouter.ai/keys>. The free tier needs no card.

2. **Add it as a secret.** Go to `Settings` -> `Secrets and variables` -> `Actions` ->
   `New repository secret`. Set Name to `OPENROUTER_API_KEY`. Paste the key into Secret.

3. **Turn on Pages.** Go to `Settings` -> `Pages` -> set `Source` to `GitHub Actions`.
   Do not pick `Deploy from a branch`. The publish workflow tries to enable this for you,
   but set it by hand if you can.

4. **Check the feeds.** Go to `Actions` -> `Validate feeds` -> `Run workflow`. Read the
   report in the run summary. Delete anything marked `FETCH_FAIL` from `feeds.txt`, and
   see the Feeds section below for what to do about `BLOCKED`.

5. **Pick a model.** The same report ends with OpenRouter's current free models. Copy an
   ID marked `yes` under JSON-SCHEMA. Go to `Settings` -> `Secrets and variables` ->
   `Actions` -> `Variables` -> `New repository variable`. Set Name to `MODEL_CHAIN` and
   Value to that ID.

6. **Run it.** Go to `Actions` -> `Weekly ToC Digest` -> `Run workflow`. Tick `dry run`
   the first time; that skips the API and proves the feeds and rendering work. Then run it
   again without `dry run`.

It then runs every Monday at 15:00 UTC, which is 08:00 PDT or 07:00 PST. GitHub cron
ignores daylight saving.

## Configuration

Four text files control everything. You should not need to touch the Python code.

| File | What it does |
|---|---|
| `interests.md` | Keywords, a narrative paragraph, section definitions |
| `prompt.txt` | Scoring rubric: what gets up- and down-weighted |
| `feeds.txt` | RSS feeds, `Name \| URL` per line |
| `pubmed_queries.txt` | PubMed queries, `Name \| query` per line |

The `## narrative` paragraph in `interests.md` influences the output more than anything
else, because it goes to the LLM verbatim on every batch. To change the focus of the digest, edit each part of that paragraph to describe the work you actually want surfaced: your populations, your designs, your methods, and what you consider rigorous. Write
it as prose rather than as a keyword list. The `## keywords` block is a cheap pre-filter
that decides which papers reach the model at all, so it should cover the vocabulary your
field uses, including abbreviations and their spelled-out forms.

Section names, thresholds, and result caps are environment variables set in
`.github/workflows/weekly-digest.yml`: `MIN_SCORE_SUICIDE`, `MIN_SCORE_SENSING`,
`MIN_SCORE_METHODS`, `MIN_SCORE_ADJACENT`, and the matching `MAX_*`. Raise a threshold if
that section gets noisy. Lower it if the section comes back empty week after week.

## What it currently tracks

| Section | Contents |
|---|---|
| Suicide & self-harm | Ideation, behavior, self-injury, crisis services, prevention and intervention trials. Pediatric and adolescent samples score higher. |
| Intensive longitudinal & sensing | EMA/ESM, digital phenotyping, passive and wearable sensing, actigraphy, JITAI, micro-randomized trials. |
| ML & dynamical systems methods | Prediction models, nonlinear time series, idiographic models, early warning signals, network psychometrics, calibration and external validation. Included even outside psychiatry if the method transfers. |
| Adjacent mental health, genetics & neurobiology | Nearby psychiatry and child mental health work, plus psychiatric genetics and neurobiology: GWAS, polygenic scores, epigenetics, neuroimaging of adolescent development, biomarkers. |

The rubric rewards external validation, prospective designs, and robust handling of rare
events. It penalizes cross-sectional self-report studies, single-site models sold as
clinically actionable, editorials, and corrections.

Scores come from a model reading a title and an abstract. It has not read the paper. Treat
a high score as "worth opening" and nothing more.

## Local use

```bash
pip install -r requirements.txt

python digest.py --dry-run             # no API calls, keyword-only scores
python digest.py --list-free-models    # what's free on OpenRouter today
python digest.py --limit 40            # one batch, for a cheap live test
python validate_feeds.py               # feed health report

export OPENROUTER_API_KEY=sk-or-v1-...
python digest.py
quarto preview
```

To compare two models, set `MODEL_CHAIN` to the first, run `--limit 40`, then swap and
repeat. Same 40 items both times, so the scores are directly comparable.

## How it works

```
feeds.txt ─────────┐
                   ├─► fetch ─► dedupe ─► keyword prefilter ─► LLM triage ─► digest.md ─► Quarto ─► Pages
pubmed_queries.txt ┘                                              ▲
                                              interests.md + prompt.txt
```

Two sources are leveraged to influence the LLM triage, because neither is enough alone. RSS gives table-of-contents coverage of
journals you specify. PubMed searches all of MEDLINE, survives publisher changes, and
reaches journals whose publishers block GitHub runners. Papers arriving from both get merged on a normalized title prefix, keeping the publisher link and whichever abstract
is longer.

Abstracts come from PubMed `efetch`. Publisher RSS feeds usually carry only "Publication date... Author(s)..." metadata
in place of an abstract, so that boilerplate is stripped and the PubMed abstract fills the
gap during the merge.

The prefilter reserves a share of its budget for each section before ranking the rest by
keyword hits. Without that, a keyword list weighted toward one topic sends almost nothing
from the other sections to the model, and those sections come back empty.

A full run is about seven API requests against a 50-per-day free allowance.

Triage tries each model in `MODEL_CHAIN` in order, remembers the first that works, and
reuses it for the remaining batches. Per model it asks for strict `json_schema` output
first, then falls back to plain JSON. A batch that fails entirely is skipped and noted in
the digest; only a total failure of every batch stops the run.

## Optional settings

| Type | Name | Purpose |
|---|---|---|
| Secret | `NCBI_API_KEY` | Raises PubMed rate limit from 3/s to 10/s. Free from your NCBI account |
| Variable | `CONTACT_EMAIL` | Identifies you to NCBI E-utilities |
| Variable | `MODEL_CHAIN` | Comma-separated model override |

Secrets are encrypted. They never appear in the repo, the logs, or the published site,
including on a public repo. Do not commit the key to a file.

## Feeds break constantly

Run `Validate feeds` monthly. It runs automatically on the 1st. The report uses five
statuses:

| Status | Meaning | What to do |
|---|---|---|
| `OK` | Parsed, has entries, recent | Nothing |
| `STALE` | Works, nothing in 120 days | Probably discontinued. Drop it |
| `BLOCKED`, HTTP 403 | Publisher refuses runner IPs | Delete the feed. Add `"J Abbrev"[Journal]` to `pubmed_queries.txt` |
| `BLOCKED`, HTML challenge | Rate limiting, if other feeds on that host passed | Raise `HOST_DELAY`. Do not delete |
| `FETCH_FAIL` | 404, DNS failure, timeout | Wrong URL or dead host. Fix or drop |

Two patterns worth knowing:

- ScienceDirect works from Actions. Use
  `https://rss.sciencedirect.com/publication/science/{ISSN}` for Elsevier titles instead of
  the journal-branded sites like thelancet.com or cell.com, which return 403.
- nature.com rate-limits bursts. Roughly eight of its twelve feeds succeed per run and
  which eight varies. `HOST_DELAY` controls the spacing, and a PubMed safety-net query
  covers those journals when a feed does get dropped.

## Retargeting this to your field

Nothing here is specific to suicide research except the text files. If you want to fork this repo and generate a digest focused on your interests/foci:

1. Rewrite `## narrative` and `## keywords` in `interests.md`.
2. Rewrite the section definitions and the up/down-weight lists in `prompt.txt`. Keep the
   section IDs in sync across `interests.md`, `prompt.txt`, and `SECTIONS` in `digest.py`.
3. Update `SECTION_KEYWORDS` and `SECTION_QUOTA` in `digest.py` so the prefilter reserves
   slots for your sections.
4. Replace `feeds.txt` and `pubmed_queries.txt`. Run `Validate feeds` before your first
   real run.
5. Start with high thresholds. Broad sections make the digest unreadable and you stop
   reading it.

## Troubleshooting

**`deploy` fails with 404.** Pages is not enabled. Go to `Settings` -> `Pages` -> set
`Source` to `GitHub Actions`, then re-run.

**Render fails.** Quarto renders every `.qmd` and `.md` in the project unless you list
files explicitly. The `render:` list in `_quarto.yml` exists to stop that. Add new pages
there.

**Digest is empty or thin.** Run `Validate feeds`. Feeds sometimes fail without errors.

**Triage batches fail, or the log shows `via openrouter/free`.** The models in
`MODEL_CHAIN` are dead. OpenRouter retires free IDs regularly. Pick a new one from the end
of the `Validate feeds` report if you see this.

Pin a specific model rather than relying on `openrouter/free`. That is an auto-router that
picks a different model per request, so scores are not comparable across batches and your
thresholds stop meaning one thing.

A model marked `no` under JSON-SCHEMA can still work, because the pipeline falls back to
plain JSON. That path is less reliable at 40 items per batch, so prefer a `yes` model, but
a much larger `no` model is worth testing against a `yes` one.

**Node.js 20 deprecation warnings.** Cosmetic. They come from GitHub's own Pages actions
and will go away when GitHub ships updated versions.

## Credit

Forked from [voytek/tocify](https://github.com/voytek/tocify) by Bradley Voytek, with
earlier work by [Renee (Hui Xin) Ng](https://github.com/nghuixin). The original idea and
scaffolding are theirs. This fork swaps in different interests, sources, scoring, and model
backend.

Maintained by Sam Sievertsen.

## License

See `LICENSE`.
