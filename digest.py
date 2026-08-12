"""
tocify: weekly journal ToC digest.

Pipeline:  RSS feeds + PubMed queries -> dedupe -> keyword prefilter
           -> LLM triage (OpenRouter) -> sectioned markdown digest

Backend is OpenRouter (OpenAI-SDK compatible). Set OPENROUTER_API_KEY.
Falls back to OPENAI_API_KEY against api.openai.com if that is what you have.

Useful CLI flags:
    python digest.py --dry-run           # no API calls; keyword-only scores
    python digest.py --list-free-models  # show current OpenRouter free models
    python digest.py --limit 40          # cap items sent to the model (cheap test)
"""

import os, re, sys, json, time, math, html, random, hashlib, argparse
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import feedparser
import httpx
from dateutil import parser as dtparser
from openai import OpenAI
from openai import APITimeoutError, APIConnectionError, RateLimitError, APIStatusError


# ---------------------------------------------------------------- config
# GitHub Actions renders an unset ${{ vars.X }} as an empty string, not as an absent
# variable. os.getenv(k, default) returns "" in that case, not the default. Every
# config read below has to treat empty as missing or the defaults silently vanish.
def _env_str(k, d=""):
    v = os.getenv(k)
    return v.strip() if v and v.strip() else d

def _env_int(k, d): return int(_env_str(k, str(d)))
def _env_float(k, d): return float(_env_str(k, str(d)))

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Free OpenRouter models rotate, and dead IDs waste free-tier requests on every run.
# This is a fallback CHAIN: the first that works wins, and the winner is then reused for
# the remaining batches. Run `python digest.py --list-free-models`, or read the tail of
# the Validate feeds report, to see what is currently available.
#
# Chosen 2026-08-11 from the models that reported strict json_schema support, ordered by
# expected instruction-following quality for this task. Size is a weak proxy for that,
# so treat the order as a starting point and not as a benchmark result.
#   nemotron-3-super  120B MoE, 12B active. Largest schema-capable free model here.
#   gpt-oss-20b       20B MoE. Reliable at structured output and instruction following.
#   gemma-4-31b       31B dense. Good fallback with a different failure profile.
#   openrouter/free   Auto-router. Last resort only: it picks a DIFFERENT model per
#                     request, so scores stop being comparable across batches.
DEFAULT_MODEL_CHAIN = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-oss-20b:free",
    "google/gemma-4-31b-it:free",
    "openrouter/free",
]
MODEL_CHAIN = [m.strip() for m in _env_str("MODEL_CHAIN", ",".join(DEFAULT_MODEL_CHAIN)).split(",") if m.strip()]
if not MODEL_CHAIN:
    raise SystemExit("MODEL_CHAIN resolved to an empty list. Unset the MODEL_CHAIN "
                     "repository variable to use the built-in defaults.")

MAX_ITEMS_PER_FEED   = _env_int("MAX_ITEMS_PER_FEED", 60)
# Safety valve only. Many publisher feeds (all the ScienceDirect ones) carry no dates,
# so their items sort last. When this cap was 600 it cut exactly those feeds, which are
# the most clinically relevant ones here. PREFILTER_KEEP_TOP does the real selection,
# and it ranks by keyword relevance rather than by date.
MAX_TOTAL_ITEMS      = _env_int("MAX_TOTAL_ITEMS", 2500)
LOOKBACK_DAYS        = _env_int("LOOKBACK_DAYS", 7)
INTERESTS_MAX_CHARS  = _env_int("INTERESTS_MAX_CHARS", 4000)
SUMMARY_MAX_CHARS    = _env_int("SUMMARY_MAX_CHARS", 500)
PREFILTER_KEEP_TOP   = _env_int("PREFILTER_KEEP_TOP", 220)
BATCH_SIZE           = _env_int("BATCH_SIZE", 40)
FEED_TIMEOUT         = _env_int("FEED_TIMEOUT", 45)
# With interleaving this is nearly free: the throttle only sleeps when other feeds have
# not already consumed the interval, so a larger floor costs little wall-clock time.
HOST_DELAY           = _env_float("HOST_DELAY", 10.0)  # min seconds between same-host hits
FEED_RETRIES         = _env_int("FEED_RETRIES", 3)     # attempts before giving up on a feed
PUBMED_RETMAX        = _env_int("PUBMED_RETMAX", 60)
PUBMED_ENABLED       = _env_str("PUBMED_ENABLED", "1") not in ("0", "false", "False")
NCBI_API_KEY         = _env_str("NCBI_API_KEY")   # optional, raises the PubMed rate limit
CONTACT_EMAIL        = _env_str("CONTACT_EMAIL")  # polite E-utilities identifier

# Per-section thresholds and caps. Tune without touching code.
SECTIONS = [
    {"id": "neurotrauma", "title": "Pediatric neurotrauma & concussion",
     "min": _env_float("MIN_SCORE_NEUROTRAUMA", 0.55), "max": _env_int("MAX_NEUROTRAUMA", 20)},
    {"id": "critcare",    "title": "Neurocritical & hospital care outcomes",
     "min": _env_float("MIN_SCORE_CRITCARE", 0.58), "max": _env_int("MAX_CRITCARE", 15)},
    {"id": "assessment",  "title": "Neuropsychological assessment & methods",
     "min": _env_float("MIN_SCORE_ASSESSMENT", 0.58), "max": _env_int("MAX_ASSESSMENT", 15)},
    {"id": "adjacent",    "title": "Adjacent developmental neuroscience",
     "min": _env_float("MIN_SCORE_ADJACENT", 0.65), "max": _env_int("MAX_ADJACENT", 10)},
]
SECTION_IDS = [s["id"] for s in SECTIONS]
UA = "tocify/2.0 (+https://github.com/SamSievertsen/tocify)"

# No "notes" field. Asking the model for free-text notes produced a repetitive wall of
# text that restated the rubric, and concatenating it across six batches then truncating
# made it worse. The digest header is written from the run's own statistics instead.
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id":      {"type": "string"},
                    "section": {"type": "string", "enum": SECTION_IDS},
                    "score":   {"type": "number"},
                    "why":     {"type": "string"},
                    "tags":    {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "section", "score", "why", "tags"],
            },
        },
    },
    "required": ["ranked"],
}


# ---------------------------------------------------------------- helpers
def sha1(s): return hashlib.sha1(s.encode("utf-8")).hexdigest()

def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def clean(s, limit=None):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


# ScienceDirect and Wiley put publication metadata in <description> instead of an
# abstract, so the "Abstract snippet" block was showing author lists. Strip the
# metadata; whatever prose survives is the real abstract, and often nothing does.
_BOILERPLATE = re.compile(
    r"(publication date\s*:|source\s*:|author\(s\)\s*:|«|abstract\s*$)", re.I)

def clean_rss_summary(s, limit=None):
    s = clean(s)
    if not s:
        return ""
    if re.match(r"^\s*publication date\s*:", s, re.I):
        # Format is: "Publication date: X Source: Y Author(s): names[. Abstract...]"
        tail = re.split(r"author\(s\)\s*:", s, flags=re.I)
        if len(tail) < 2:
            return ""
        rest = tail[1]
        # Author lists have no sentence structure. The abstract, if present, starts at
        # the first sentence long enough to be prose.
        sentences = re.split(r"(?<=[.!?])\s+", rest)
        prose = [x for x in sentences if len(x) > 80 and x.count(",") < len(x) / 25]
        s = " ".join(prose).strip()
    if _BOILERPLATE.fullmatch(s.strip()):
        return ""
    return clean(s, limit)

def load_pairs(path):
    """Parse 'Name | value' lines, skipping blanks and # comments."""
    out = []
    if not os.path.exists(path):
        return out
    for line in read_text(path).splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "|" in s:
            name, val = [x.strip() for x in s.split("|", 1)]
        else:
            name, val = None, s
        if val:
            out.append({"name": name, "value": val})
    return out

def section_of(md, heading):
    m = re.search(rf"(?im)^\s*#{{1,6}}\s+{re.escape(heading)}\s*$", md)
    if not m:
        return ""
    rest = md[m.end():]
    m2 = re.search(r"(?im)^\s*#{1,6}\s+\S", rest)
    return (rest[:m2.start()] if m2 else rest).strip()

def parse_interests(md):
    keywords = []
    for line in section_of(md, "keywords").splitlines():
        line = re.sub(r"^[\-\*\+]\s+", "", line.strip())
        if line and not line.startswith("<!--"):
            keywords.append(line)
    narrative = section_of(md, "narrative").strip()
    if len(narrative) > INTERESTS_MAX_CHARS:
        narrative = narrative[:INTERESTS_MAX_CHARS] + "…"
    return {"keywords": keywords[:250], "narrative": narrative}


# ---------------------------------------------------------------- RSS
def parse_date(entry):
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    for key in ("published", "updated", "created", "dc_date"):
        val = entry.get(key)
        if val:
            try:
                dt = dtparser.parse(val)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass
    return None

_last_hit = {}

def _throttle(url):
    """Space out requests to the same host. Publishers serve an HTML challenge page
    instead of the feed when hit too fast, and that looks exactly like a dead feed.
    nature.com hosts a dozen feeds here, so this matters."""
    host = urllib.parse.urlparse(url).netloc
    wait = HOST_DELAY - (time.monotonic() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


def _get(url):
    _throttle(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return urllib.request.urlopen(req, timeout=FEED_TIMEOUT).read()


def fetch_one_feed(feed, cutoff):
    url, out = feed["value"], []
    label = feed.get("name") or url
    d = None
    for attempt in range(FEED_RETRIES):
        try:
            raw = _get(url)
        except Exception as e:
            print(f"  ! {label}: fetch failed ({type(e).__name__}: {str(e)[:60]})")
            return out
        d = feedparser.parse(raw)
        if d.entries:
            break
        head = raw[:400].decode("utf-8", "ignore").lower()
        is_challenge = "<html" in head or "<!doctype html" in head
        if is_challenge and attempt < FEED_RETRIES - 1:
            # Rate-limit challenge page. Nature's window is long, so back off hard.
            time.sleep(15 * (attempt + 1) + random.random() * 5)
            continue
        break

    if not d or not d.entries:
        print(f"  ! {label}: 0 entries (run Validate feeds)")
        return out

    source = (feed.get("name") or d.feed.get("title") or url).strip()
    kept = 0
    for e in d.entries[:MAX_ITEMS_PER_FEED]:
        title = clean(e.get("title", ""))
        link = (e.get("link") or "").strip()
        if not (title and link):
            continue
        dt = parse_date(e)
        if dt and dt < cutoff:
            continue
        out.append({
            "id": sha1(f"{title}|{link}"),
            "source": source,
            "title": title,
            "link": link,
            "published_utc": dt.isoformat() if dt else None,
            "summary": clean_rss_summary(e.get("summary") or e.get("description") or "", SUMMARY_MAX_CHARS),
        })
        kept += 1
    print(f"  · {source}: {kept} new / {len(d.entries)} in feed")
    return out

def interleave_by_host(feeds):
    """Round-robin the feed order across hosts.

    feeds.txt groups journals by topic, which means a dozen consecutive nature.com
    requests. Nature rate-limits bursts and answers with an HTML challenge page, so a
    random handful of its feeds come back empty every run. Interleaving puts roughly a
    dozen other requests between any two hits on the same host, which spaces them out
    using time we were going to spend anyway instead of with sleep().
    """
    buckets = {}
    for f in feeds:
        buckets.setdefault(urllib.parse.urlparse(f["value"]).netloc, []).append(f)
    order, queues = [], list(buckets.values())
    while queues:
        for q in list(queues):
            order.append(q.pop(0))
            if not q:
                queues.remove(q)
    return order


def fetch_rss(feeds):
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    items = []
    ordered = interleave_by_host(feeds)
    hosts = len({urllib.parse.urlparse(f["value"]).netloc for f in feeds})
    print(f"Fetching {len(feeds)} RSS feeds across {hosts} hosts, interleaved "
          f"(lookback {LOOKBACK_DAYS}d)…")
    for f in ordered:
        items.extend(fetch_one_feed(f, cutoff))
    undated = sum(1 for i in items if not i["published_utc"])
    if undated:
        print(f"  ({undated} items carry no date; LOOKBACK_DAYS cannot filter those)")
    return items


# ---------------------------------------------------------------- PubMed
def _eutils(endpoint, params):
    params = dict(params)
    params["tool"] = "tocify"
    if CONTACT_EMAIL:
        params["email"] = CONTACT_EMAIL
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=FEED_TIMEOUT).read()

def parse_pubmed_xml(raw):
    """Pull title, abstract, journal and date out of an efetch PubmedArticleSet."""
    out = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  ! PubMed XML parse failed: {e}")
        return out

    for art in root.findall(".//PubmedArticle"):
        pmid = art.findtext(".//MedlineCitation/PMID")
        title_el = art.find(".//Article/ArticleTitle")
        if pmid is None or title_el is None:
            continue
        title = clean("".join(title_el.itertext()))
        if not title:
            continue

        # Structured abstracts split into labelled sections. Keep the labels, they
        # tell the model which part is Methods and which is Results.
        parts = []
        for ab in art.findall(".//Article/Abstract/AbstractText"):
            text = "".join(ab.itertext()).strip()
            if not text:
                continue
            label = (ab.get("Label") or "").strip()
            parts.append(f"{label}: {text}" if label else text)
        summary = clean(" ".join(parts), SUMMARY_MAX_CHARS)

        journal = (art.findtext(".//Journal/ISOAbbreviation")
                   or art.findtext(".//Journal/Title") or "PubMed")

        pub = None
        y = art.findtext(".//Article/ArticleDate/Year") or art.findtext(".//JournalIssue/PubDate/Year")
        m = art.findtext(".//Article/ArticleDate/Month") or art.findtext(".//JournalIssue/PubDate/Month") or "1"
        dday = art.findtext(".//Article/ArticleDate/Day") or "1"
        if y:
            try:
                pub = dtparser.parse(f"{y}-{m}-{dday}").replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                pass

        link = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        out.append({
            "id": sha1(f"{title}|{link}"),
            "source": f"{journal} (PubMed)",
            "title": title,
            "link": link,
            "published_utc": pub,
            "summary": summary,
        })
    return out


def fetch_pubmed(queries):
    """Run each saved query against PubMed, restricted to the lookback window."""
    if not queries:
        return []
    items = []
    print(f"Querying PubMed ({len(queries)} saved queries, reldate {LOOKBACK_DAYS}d)…")
    for q in queries:
        name = q.get("name") or q["value"][:40]
        try:
            raw = _eutils("esearch.fcgi", {
                "db": "pubmed", "term": q["value"], "retmode": "json",
                "retmax": PUBMED_RETMAX, "reldate": LOOKBACK_DAYS, "datetype": "edat",
            })
            ids = json.loads(raw).get("esearchresult", {}).get("idlist", [])
            if not ids:
                print(f"  · PubMed: {name}: 0 hits")
                continue
            time.sleep(0.4)  # respect NCBI rate limits (3/s without a key)
            # efetch, not esummary. esummary carries no abstract, which forced the model
            # to score from titles alone and produced "Title indicates..." for everything.
            raw = _eutils("efetch.fcgi", {
                "db": "pubmed", "id": ",".join(ids), "retmode": "xml",
            })
            got = parse_pubmed_xml(raw)
            items.extend(got)
            with_abs = sum(1 for g in got if g["summary"])
            print(f"  · PubMed: {name}: {len(got)} hits ({with_abs} with abstracts)")
            time.sleep(0.4)
        except Exception as e:
            print(f"  ! PubMed query '{name}' failed ({type(e).__name__}: {str(e)[:70]})")
    return items


def title_key(title):
    """Normalised prefix used to spot the same paper arriving from two sources.

    PubMed keeps the full subtitle where a publisher feed often drops it, so
    "Preventing Firearm-Related Suicide Deaths Among Youth" and
    "Preventing Firearm-Related Suicide Deaths Among Youth-Action Is the Antidote to
    Despair." are one paper. Comparing whole titles missed that. A 40-character
    normalised prefix catches it and is still specific enough to avoid collisions.
    """
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:40]


def merge_records(a, b):
    """Combine two records for the same paper, keeping the best field from each."""
    pubmed_a, pubmed_b = "(PubMed)" in a["source"], "(PubMed)" in b["source"]
    # Prefer the publisher record for link and source: it goes to the article itself.
    primary, other = (b, a) if (pubmed_a and not pubmed_b) else (a, b)
    merged = dict(primary)
    # But take whichever abstract is actually longer. PubMed usually wins here, because
    # publisher feeds often carry only "Publication date... Author(s)..." boilerplate.
    if len(other.get("summary") or "") > len(merged.get("summary") or ""):
        merged["summary"] = other["summary"]
    merged["published_utc"] = merged.get("published_utc") or other.get("published_utc")
    if primary is not other and pubmed_a != pubmed_b:
        merged["source"] = f"{primary['source']} / PubMed"
    return merged


def dedupe(items):
    """Collapse duplicates by id, then by normalised title prefix, merging fields."""
    by_id, by_title = {}, {}
    for it in items:
        if it["id"] in by_id:
            continue
        key = title_key(it["title"])
        if key in by_title:
            prev = by_title[key]
            merged = merge_records(prev, it)
            by_id.pop(prev["id"], None)
            by_id[merged["id"]] = merged
            by_title[key] = merged
            continue
        by_id[it["id"]] = it
        by_title[key] = it
    out = list(by_id.values())
    out.sort(key=lambda x: x["published_utc"] or "", reverse=True)
    return out[:MAX_TOTAL_ITEMS]


# ---------------------------------------------------------------- prefilter
JUNK = re.compile(
    r"^(correction|corrigend|erratum|errata|retraction|editorial|error in|"
    r"author correction|publisher correction|in this issue|this month in|masthead|"
    r"issue information|book review|table of contents|front matter|back matter|"
    r"acknowledg|highlights|cover image|editor'?s? (choice|pick))", re.I)

def keyword_hits(it, kws):
    text = (it.get("title", "") + " " + it.get("summary", "")).lower()
    return sum(1 for k in kws if k in text)

# Terms that mark an item as a candidate for each section. Used to give every section a
# guaranteed share of the prefilter, and to fake scores in --dry-run.
SECTION_KEYWORDS = {
    "neurotrauma": ["traumatic brain injury", " tbi", "mtbi", "concussion",
                    "post-concussi", "head injury", "head trauma", "neurotrauma",
                    "abusive head", "return to play", "return to learn",
                    "skull fracture", "contusion", "intracranial"],
    "critcare":    ["intensive care", "critical care", "picu", "critically ill",
                    "cardiac arrest", "ecmo", "sepsis", "delirium", "encephalopath",
                    "hypoxic", "ischemic", "stroke", "encephalitis", "meningitis",
                    "length of stay", "hospitali", "mechanical ventilation",
                    "functional outcome", "neurodevelopmental outcome"],
    "assessment":  ["neuropsycholog", "normative", "psychometric", "validity",
                    "reliability", "test-retest", "reliable change", "practice effect",
                    "performance validity", "symptom validity", "measurement invariance",
                    "factor structure", "factor analysis", "standardization",
                    "cognitive screening", "executive function", "processing speed",
                    "working memory", "intelligence test", "teleneuropsych"],
    "adjacent":    ["development", "neuroimaging", "mri", "diffusion", "white matter",
                    "cortical", "connectom", "epilepsy", "seizure", "cerebral palsy",
                    "preterm", "prematur", "congenital", "genetic", "biomarker",
                    "plasticity", "recovery"],
}

# Share of the prefilter budget reserved for each section. Without this the budget goes
# almost entirely to `suicide`, because the keyword list is suicide-heavy, and the other
# three sections come back empty. Weights need not sum to 1; leftovers go to the general
# pool ranked purely by keyword hits.
SECTION_QUOTA = {"neurotrauma": 0.35, "critcare": 0.20, "assessment": 0.20, "adjacent": 0.10}


def prefilter(items, keywords, keep_top):
    kws = [k.lower().strip() for k in keywords if k.strip()]
    live = [it for it in items if not JUNK.match(it["title"])]
    dropped = len(items) - len(live)
    if dropped:
        print(f"Dropped {dropped} editorial/correction items")

    scored = sorted(((keyword_hits(it, kws), it) for it in live),
                    key=lambda p: p[0], reverse=True)

    chosen, seen = [], set()

    def take(pool, n):
        got = 0
        for it in pool:
            if got >= n:
                break
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            chosen.append(it)
            got += 1
        return got

    # Reserved slots first, so a quiet section still reaches the model.
    for sec, share in SECTION_QUOTA.items():
        terms = SECTION_KEYWORDS[sec]
        pool = [it for _h, it in scored
                if any(t in (it["title"] + " " + it["summary"]).lower() for t in terms)]
        n = take(pool, int(keep_top * share))
        print(f"  prefilter: {sec} reserved {n}/{int(keep_top * share)} "
              f"({len(pool)} candidates)")

    # Then fill the remainder by raw keyword rank.
    take([it for _h, it in scored], keep_top - len(chosen))
    return chosen[:keep_top]


# ---------------------------------------------------------------- LLM
def make_client():
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        base, label = OPENROUTER_BASE, "OpenRouter"
        headers = {"HTTP-Referer": "https://github.com/SamSievertsen/tocify", "X-Title": "tocify"}
    else:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "No API key. Set OPENROUTER_API_KEY (free tier, no card: "
                "https://openrouter.ai/keys) or OPENAI_API_KEY.")
        base, label, headers = None, "OpenAI", {}
    print(f"Backend: {label}")
    http_client = httpx.Client(
        timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
        trust_env=False, headers={"Connection": "close"},
    )
    kw = {"api_key": key, "http_client": http_client, "default_headers": headers}
    if base:
        kw["base_url"] = base
    return OpenAI(**kw), label


def list_free_models():
    """Print OpenRouter models that cost nothing and support structured outputs."""
    req = urllib.request.Request(f"{OPENROUTER_BASE}/models", headers={"User-Agent": UA})
    data = json.loads(urllib.request.urlopen(req, timeout=60).read())
    rows = []
    for m in data.get("data", []):
        p = m.get("pricing", {}) or {}
        free = all(float(p.get(k, 0) or 0) == 0 for k in ("prompt", "completion"))
        if not free:
            continue
        params = m.get("supported_parameters", []) or []
        rows.append((m["id"], "structured_outputs" in params or "response_format" in params,
                     m.get("context_length", 0)))
    rows.sort(key=lambda r: (not r[1], r[0]))
    print(f"{'MODEL ID':<52}{'JSON-SCHEMA':<13}CONTEXT")
    for mid, so, ctx in rows:
        print(f"{mid:<52}{'yes' if so else 'no':<13}{ctx:,}")
    print(f"\n{len(rows)} free models; {sum(1 for r in rows if r[1])} support structured outputs.")
    print("Set MODEL_CHAIN (comma-separated) to override the default chain.")


class ModelUnavailable(RuntimeError):
    """The provider accepted the request but returned no usable completion."""


def extract_json(text):
    """Models sometimes wrap JSON in prose or fences. Recover it."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        depth, start = 0, None
        for i, ch in enumerate(text):
            if ch == opener:
                if depth == 0:
                    start = i
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        start = None
    raise ValueError("no parseable JSON in model output")


def normalize_result(obj):
    """Coerce whatever the model returned into {"ranked": [...]}.

    In plain-JSON mode (no strict schema) models frequently return a bare array, or
    wrap the array under a key of their own choosing. Without this, triage() calls
    .get() on a list and the whole run dies with an AttributeError.
    """
    if isinstance(obj, list):
        return {"ranked": obj}
    if isinstance(obj, dict):
        if isinstance(obj.get("ranked"), list):
            return {"ranked": obj["ranked"]}
        for k in ("items", "results", "data", "articles", "output", "papers"):
            if isinstance(obj.get(k), list):
                return {"ranked": obj[k]}
        if {"id", "section", "score"} <= set(obj):
            return {"ranked": [obj]}          # single item returned bare
        lists = [v for v in obj.values() if isinstance(v, list)]
        if len(lists) == 1:
            return {"ranked": lists[0]}
    raise ValueError(f"unexpected JSON shape from model: {type(obj).__name__}")


def call_model(client, model, prompt, use_schema=True):
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    if use_schema:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "weekly_toc_digest", "strict": True, "schema": SCHEMA},
        }
    resp = client.chat.completions.create(**kwargs)

    # OpenRouter returns errors with HTTP 200 and an error object in the body. The SDK
    # parses that into a response whose .choices is None, so indexing it blows up with
    # a TypeError far from the real cause. Surface the actual message instead.
    if not getattr(resp, "choices", None):
        err = getattr(resp, "error", None)
        if err is None and getattr(resp, "model_extra", None):
            err = resp.model_extra.get("error")
        raise ModelUnavailable(f"{model}: no choices returned. Provider said: {err!r}")

    content = resp.choices[0].message.content
    if not content or not content.strip():
        raise ModelUnavailable(f"{model}: returned empty content")
    return normalize_result(extract_json(content))


# Remembers the first model that actually worked. Without this, every batch re-tries
# the dead entries at the front of the chain, which wastes free-tier requests.
_working = {"model": None, "schema": None}


def triage_batch(client, prompt):
    """Try each model in the chain. For each, try schema mode then plain-JSON mode."""
    plan = []
    if _working["model"]:
        plan.append((_working["model"], _working["schema"]))
    for model in MODEL_CHAIN:
        for use_schema in (True, False):
            if (model, use_schema) != (_working["model"], _working["schema"]):
                plan.append((model, use_schema))

    last = None
    for model, use_schema in plan:
        for attempt in range(3):
            try:
                out = call_model(client, model, prompt, use_schema)
                if (model, use_schema) != (_working["model"], _working["schema"]):
                    print(f"    (via {model}, schema={use_schema})")
                    _working.update(model=model, schema=use_schema)
                return out
            except (APITimeoutError, APIConnectionError, RateLimitError) as e:
                last = e
                time.sleep(min(45, 3 * 2 ** attempt))
            except Exception as e:
                # Model missing, schema unsupported, bad JSON, empty choices. All mean
                # "try something else" rather than "crash the run".
                last = e
                if _working["model"] == model:
                    _working.update(model=None, schema=None)
                break
    raise RuntimeError(f"All models failed for this batch. Last error: "
                       f"{type(last).__name__}: {last}")


def triage(client, interests, items, template):
    total = math.ceil(len(items) / BATCH_SIZE)
    ranked, warnings = [], []
    failed = 0
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        n = i // BATCH_SIZE + 1
        print(f"  Triage batch {n}/{total} ({len(batch)} items)")
        lean = [{"id": it["id"], "source": it["source"], "title": it["title"],
                 "summary": it["summary"][:SUMMARY_MAX_CHARS]} for it in batch]
        prompt = (template
                  .replace("{{KEYWORDS}}", json.dumps(interests["keywords"], ensure_ascii=False))
                  .replace("{{NARRATIVE}}", interests["narrative"])
                  .replace("{{ITEMS}}", json.dumps(lean, ensure_ascii=False)))
        try:
            res = triage_batch(client, prompt)
        except Exception as e:
            # A partial digest beats no digest. Skip this batch and keep going.
            failed += 1
            print(f"    ! batch {n} failed, skipping: {e}")
            continue
        ranked.extend(res.get("ranked", []))

    if failed == total:
        raise RuntimeError(f"All {total} triage batches failed. Check the errors above, "
                           f"then run `python digest.py --list-free-models` and set the "
                           f"MODEL_CHAIN repository variable to a model that works.")
    if failed:
        warnings.append(f"{failed} of {total} triage batches failed, so about "
                        f"{failed * BATCH_SIZE} items went unscored this week.")

    best = {}
    for r in ranked:
        rid = r.get("id")
        if not rid:
            continue
        try:
            r["score"] = float(r.get("score", 0))
        except (TypeError, ValueError):
            r["score"] = 0.0
        if r.get("section") not in SECTION_IDS:
            r["section"] = "adjacent"
        if rid not in best or r["score"] > best[rid]["score"]:
            best[rid] = r
    return {"warnings": warnings, "ranked": list(best.values())}


def dry_run_triage(interests, items):
    """Keyword-only scoring so the pipeline can be tested with no API key."""
    kws = [k.lower() for k in interests["keywords"]]
    sec_kw = SECTION_KEYWORDS
    ranked = []
    for it in items:
        text = (it["title"] + " " + it["summary"]).lower()
        sec = "adjacent"
        for s, terms in sec_kw.items():
            if any(t in text for t in terms):
                sec = s
                break
        h = keyword_hits(it, kws)
        ranked.append({"id": it["id"], "section": sec,
                       "score": round(min(0.99, 0.30 + 0.12 * h), 2),
                       "why": f"DRY RUN. Keyword-only score, {h} keyword matches. No model was called.",
                       "tags": ["dry-run"]})
    return {"warnings": ["DRY RUN. Scores are keyword counts, not model judgements."],
            "ranked": ranked}


# ---------------------------------------------------------------- render
def render(result, items_by_id, stats):
    week_of = datetime.now(timezone.utc).date().isoformat()
    ranked = result.get("ranked", [])
    warnings = result.get("warnings", [])

    buckets = {s["id"]: [] for s in SECTIONS}
    for r in ranked:
        if r["id"] in items_by_id:
            buckets[r["section"]].append(r)

    kept = {}
    for s in SECTIONS:
        rows = sorted(buckets[s["id"]], key=lambda x: x["score"], reverse=True)
        kept[s["id"]] = [r for r in rows if r["score"] >= s["min"]][:s["max"]]

    total_kept = sum(len(v) for v in kept.values())
    kept_items = [items_by_id[r["id"]] for v in kept.values() for r in v]
    with_abstract = sum(1 for it in kept_items if it.get("summary"))
    journals = len({it["source"].replace(" / PubMed", "") for it in kept_items})

    L = [f"# Weekly ToC Digest, week of {week_of}", ""]
    L += [
        "New papers on suicidality, intensive longitudinal data, and computational "
        "methods, scanned automatically each Monday and ranked against the "
        "[interests](interests.html) that drive this digest. Scores are a language "
        "model's judgement from the title and abstract only, so read them as triage "
        "and not as appraisal.",
        "",
    ]
    for w in warnings:
        L += [f"> {w}", ""]
    L += ["| Section | Kept | Threshold |", "|---|---:|---:|"]
    for s in SECTIONS:
        L.append(f"| {s['title']} | {len(kept[s['id']])} | ≥ {s['min']:.2f} |")
    L += ["",
          f"*{total_kept} kept from {stats['scored']} scored, out of {stats['fetched']} "
          f"gathered across {stats['feeds']} journal feeds and {stats['queries']} PubMed "
          f"queries in the last {LOOKBACK_DAYS} days. "
          f"Spanning {journals} sources; {with_abstract} of {total_kept} include an abstract.*",
          "", "---", ""]

    if total_kept == 0:
        L += ["_Nothing met threshold this week._", ""]
        return "\n".join(L)

    for s in SECTIONS:
        rows = kept[s["id"]]
        if not rows:
            continue
        L += [f"## {s['title']}", ""]
        for r in rows:
            it = items_by_id[r["id"]]
            L += [f"### [{it['title']}]({it['link']})", ""]
            meta = [f"*{it['source']}*", f"**{r['score']:.2f}**"]
            if it.get("published_utc"):
                meta.append(it["published_utc"][:10])
            L += [" · ".join(meta), ""]
            if r.get("tags"):
                L += ["`" + "` `".join(t for t in r["tags"][:6]) + "`", ""]
            L += [clean(r.get("why", "")), ""]
            if it.get("summary"):
                L += ["<details><summary>Abstract snippet</summary>", "",
                      it["summary"], "", "</details>", ""]
        L += ["---", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no API calls; keyword-only scoring")
    ap.add_argument("--list-free-models", action="store_true", help="list free OpenRouter models")
    ap.add_argument("--limit", type=int, default=0, help="cap items sent to the model")
    args = ap.parse_args()

    if args.list_free_models:
        list_free_models()
        return

    interests = parse_interests(read_text("interests.md"))
    print(f"Interests: {len(interests['keywords'])} keywords, "
          f"{len(interests['narrative'])} chars of narrative")

    feeds = load_pairs("feeds.txt")
    queries = load_pairs("pubmed_queries.txt") if PUBMED_ENABLED else []

    items = fetch_rss(feeds)
    items += fetch_pubmed(queries)
    items = dedupe(items)
    print(f"\n{len(items)} unique items after dedupe")

    week_of = datetime.now(timezone.utc).date().isoformat()
    if not items:
        with open("digest.md", "w", encoding="utf-8") as f:
            f.write(f"# Weekly ToC Digest, week of {week_of}\n\n"
                    f"_No items found in the last {LOOKBACK_DAYS} days. "
                    f"If this repeats, run the **Validate feeds** workflow. "
                    f"feed URLs rot._\n")
        print("No items; wrote digest.md")
        return

    gathered = len(items)   # count before the prefilter, so the header is honest
    items = prefilter(items, interests["keywords"], PREFILTER_KEEP_TOP)
    if args.limit:
        items = items[:args.limit]
    have_abs = sum(1 for it in items if it["summary"])
    print(f"{len(items)} items to triage ({have_abs} with an abstract, "
          f"{len(items) - have_abs} title-only)\n")

    items_by_id = {it["id"]: it for it in items}
    stats = {"fetched": gathered, "scored": len(items),
             "feeds": len(feeds), "queries": len(queries)}

    if args.dry_run:
        result = dry_run_triage(interests, items)
    else:
        client, _ = make_client()
        result = triage(client, interests, items, read_text("prompt.txt"))

    md = render(result, items_by_id, stats)
    with open("digest.md", "w", encoding="utf-8") as f:
        f.write(md)
    print(f"\nWrote digest.md ({len(md):,} chars)")


if __name__ == "__main__":
    main()
