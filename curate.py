#!/usr/bin/env python3
"""Daily Reads — curates 12 articles a day and publishes them as an RSS feed.

Reads the latest articles from the publications in sources.txt, then Gemini (free tier,
no web search) acts as editor and picks the best 12 using prompt.txt.
Curator chain: Gemini (main model) -> Gemini (lite model) -> balanced picker without AI.
Every link is opened and checked before it is published. Repeats are blocked against
everything ever sent. The feed keeps the last KEEP_DAYS days.

Runs on GitHub Actions (see .github/workflows/daily.yml). Needs one secret: GEMINI_API_KEY.
"""
import datetime as dt
import difflib
import html
import json
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, urljoin
from html.parser import HTMLParser

import requests

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "state" / "history.json"
CHATGPT_INBOX = ROOT / "state" / "chatgpt_picks.json"
FEED = ROOT / "docs" / "feed.xml"
PROMPT = ROOT / "prompt.txt"
SOURCES = ROOT / "sources.txt"

FEED_TITLE = os.environ.get("FEED_TITLE", "Daily Reads")
FEED_LINK = os.environ.get("FEED_LINK", "https://fgdgfgf.github.io/daily-reads/").lower()  # set by the workflow
TZ = dt.timezone(dt.timedelta(hours=4))  # Mauritius
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "60"))
SHORT_N, LONG_N = 10, 2
MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-flash-latest,gemini-flash-lite-latest,gemini-3.1-flash-lite").split(",") if m.strip()]
BLOCKED_DOMAINS = {"statnews.com"}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_src|cmpid|smid|ito$|soc_|ocid|at_medium|at_campaign|cmp$)", re.I)
CATEGORIES = {"weird_history", "fraud_investigation", "science_tech", "culture_ideas",
              "islamic", "wildcard", "rotating"}

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "en;q=0.9"})


LOG = []


def log(*a):
    line = " ".join(str(x) for x in a)
    LOG.append(line)
    print(line, flush=True)


def save_log():
    """Keep the latest run's log in the repo (state/last-run.log) so it's easy to check."""
    (ROOT / "state").mkdir(exist_ok=True)
    stamp = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    (ROOT / "state" / "last-run.log").write_text(f"Run at {stamp} (Mauritius)\n" + "\n".join(LOG) + "\n")


# ── helpers ───────────────────────────────────────────────────────────────────
def clean_text(s, limit=600):
    """Untrusted text -> plain single-line text (no tags, no control chars)."""
    s = html.unescape(str(s or ""))
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def domain(url):
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def normalize_url(url):
    p = urlsplit(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)]
    path = re.sub(r"/amp/?$", "", p.path) or "/"
    path = path.rstrip("/") or "/"
    return urlunsplit(("https", p.hostname.lower(), path, urlencode(q), ""))


def dedup_key(url):
    """Same article at http/https, with/without www or tracking → same key."""
    n = normalize_url(url) or url
    return n.replace("://www.", "://", 1)


def norm_title(t):
    return re.sub(r"[^a-z0-9 ]", "", clean_text(t).lower())


def similar(a, b):
    return difflib.SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()


def words(t):
    return {w for w in norm_title(t).split() if len(w) > 3}


# ── history ───────────────────────────────────────────────────────────────────
def load_history():
    if HISTORY.exists():
        return json.loads(HISTORY.read_text())
    return []


def is_repeat(item, history, extra=()):
    """Same link as anything sent before, or a near-identical headline in the recent past."""
    u = item["norm"]
    if any(h.get("norm") == u for h in history) or any(h.get("norm") == u for h in extra):
        return True
    t = norm_title(item["title"])
    for h in list(history[-600:]) + list(extra):
        m = difflib.SequenceMatcher(None, norm_title(h["title"]), t)
        if m.real_quick_ratio() > 0.85 and m.quick_ratio() > 0.85 and m.ratio() > 0.85:
            return True
    return False


# ── link verification ─────────────────────────────────────────────────────────
def page_title(text):
    for pat in (r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                r"<title[^>]*>(.*?)</title>"):
        m = re.search(pat, text, re.I | re.S)
        if m:
            return clean_text(m.group(1), 300)
    return ""


VERIFY = {"reason": "", "blocked": False}


def verify(url, claimed_title, trusted=False):
    """Returns the final canonical URL if the link really points at that article, else None.
    The reason for a rejection is left in VERIFY["reason"] (and VERIFY["blocked"] for bot walls)."""
    VERIFY["reason"], VERIFY["blocked"] = "", False
    try:
        r = session.get(url, timeout=20, allow_redirects=True, stream=True)
        body = r.raw.read(400_000, decode_content=True).decode(r.encoding or "utf-8", "ignore")
        r.close()
    except Exception as e:
        VERIFY["reason"] = f"site unreachable ({type(e).__name__})"
        log(f"   ✗ unreachable {url} ({type(e).__name__})")
        return None
    final = normalize_url(r.url) or url
    if domain(final) in BLOCKED_DOMAINS:
        VERIFY["reason"] = "blocked publisher"
        return None
    path = urlsplit(final).path
    if path in ("", "/") or len(path) < 6:
        VERIFY["reason"] = "link redirects to a homepage, not the article"
        log(f"   ✗ lands on a homepage {url}")
        return None
    if r.status_code in (401, 403, 429):
        # Bot-protected site: accept only if Google's search results pointed at it.
        if trusted:
            return final
        VERIFY["reason"], VERIFY["blocked"] = f"publisher blocks automated checks (HTTP {r.status_code})", True
        VERIFY["final"] = final
        log(f"   ✗ blocked ({r.status_code}) and unconfirmed {url}")
        return None
    if r.status_code >= 400:
        VERIFY["reason"] = f"broken link (HTTP {r.status_code})"
        log(f"   ✗ HTTP {r.status_code} {url}")
        return None
    t = page_title(body)
    if t and claimed_title and not trusted:  # links straight from a publisher's feed are already exact
        overlap = words(t) & words(claimed_title)
        if similar(t, claimed_title) < 0.35 and len(overlap) < 2:
            VERIFY["reason"] = f"page title doesn't match the claimed headline (page says {t[:70]!r})"
            log(f"   ✗ page title doesn't match ({t[:60]!r}) {url}")
            return None
    return final


# ── prompt ─────────────────────────────────────────────────────────
def build_prompt(history):
    today = dt.datetime.now(TZ).strftime("%A %d %B %Y")
    recent = [h for h in history][-90:]
    articles = "\n".join(f"  - {h['title']} ({h['publisher']})" for h in recent) or "  (none yet)"
    themes = [h.get("theme") for h in reversed(history) if h.get("theme")]
    seen, recent_themes = set(), []
    for t in themes:
        if t not in seen:
            seen.add(t); recent_themes.append(t)
        if len(recent_themes) == 3:
            break
    return (PROMPT.read_text()
            .replace("{{TODAY}}", today)
            .replace("{{RECENT_ARTICLES}}", articles)
            .replace("{{RECENT_THEMES}}", ", ".join(recent_themes) or "none"))


def gemini(model, prompt, key):
    """Plain Gemini call (no web search — that isn't available on the free tier)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"}}
    r = requests.post(url, params={"key": key}, json=body, timeout=300)
    if r.status_code != 200:
        try:
            err = r.json().get("error", {})
            msg = err.get("message", "")[:200]
            details = [d.get("quotaId") or d.get("reason") for d in err.get("details", []) if isinstance(d, dict)]
            raise RuntimeError(f"HTTP {r.status_code}: {msg} {[x for x in details if x]}")
        except ValueError:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    cand = (r.json().get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise RuntimeError("no JSON array in reply")
    return json.loads(m.group(0))


# ── the candidate pool: latest articles from the publications in sources.txt ──
def parse_date(s):
    from email.utils import parsedate_to_datetime
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def parse_feed(xml_text):
    out = []
    root = ET.fromstring(xml_text)
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        get = lambda *names: next((c for c in el if c.tag.split("}")[-1] in names), None)
        t, l = get("title"), get("link")
        d = get("description")
        if d is None:
            d = get("summary")
        when = get("pubDate", "published", "updated", "date")
        link = (l.get("href") if l is not None and l.get("href") else (l.text if l is not None else "")) or ""
        out.append({"title": clean_text(t.text if t is not None else ""),
                    "url": link.strip(),
                    "summary": clean_text(d.text if d is not None else "", 220),
                    "when": parse_date(when.text if when is not None else "")})
    return out


def read_sources():
    rows = []
    for line in SOURCES.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            cat, long_, url = [p.strip() for p in line.split("|", 2)]
        except ValueError:
            continue
        rows.append((cat, long_ == "yes", url))
    return rows


def fetch_one(row):
    cat, is_long, url = row
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        return row, parse_feed(r.content), None
    except Exception as e:
        return row, [], type(e).__name__


def collect_pool(history, per_feed=6, max_age_days=60):
    from concurrent.futures import ThreadPoolExecutor
    rows = read_sources()
    log(f"Reading {len(rows)} publications…")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)
    pool, ok, failed = [], 0, []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for (cat, is_long, url), entries, err in ex.map(fetch_one, rows):
            if err:
                failed.append(f"{domain(url)} ({err})")
                continue
            ok += 1
            n_added = 0
            for e in entries:
                if n_added >= per_feed:
                    break
                n = normalize_url(e["url"]) if e["url"] else None
                if not n or not e["title"] or domain(n) in BLOCKED_DOMAINS:
                    continue
                if e["when"] and e["when"] < cutoff:
                    continue
                item = {"title": e["title"], "url": n, "norm": dedup_key(n), "publisher": domain(n),
                        "date": e["when"].strftime("%Y-%m-%d") if e["when"] else "",
                        "category": cat, "theme": "", "long": is_long, "hook": e["summary"],
                        "trusted": True, "alts": [], "source": "backup"}
                if is_repeat(item, history):
                    continue
                pool.append(item)
                n_added += 1
    log(f"  {ok} publications read, {len(pool)} fresh candidates")
    if failed:
        log("  couldn't read: " + ", ".join(failed))
    random.shuffle(pool)
    for i, p in enumerate(pool):
        p["id"] = i + 1
    return pool


# ── curator 1: Gemini picks the best from the pool ────────────────────────────
def from_gemini(pool, history):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        log("No GEMINI_API_KEY set — skipping Gemini.")
        return []
    if not pool:
        return []
    lines = "\n".join(
        f"{p['id']} | {p['publisher']} | {p['date'] or 'undated'} | "
        f"{'long-form outlet' if p['long'] else 'regular'} | {p['title']} | {p['hook'][:200]}"
        for p in pool)
    prompt = build_prompt(history).replace("{{CANDIDATES}}", lines)
    by_id = {p["id"]: p for p in pool}
    for model in MODELS:
        for attempt in range(1, 4):
            try:
                log(f"Gemini {model}, attempt {attempt}…")
                raw = gemini(model, prompt, key)
                picks = []
                for x in raw:
                    if not isinstance(x, dict):
                        continue
                    try:
                        p = dict(by_id[int(x.get("id"))])
                    except (KeyError, TypeError, ValueError):
                        continue
                    cat = str(x.get("category", p["category"])).strip()
                    p["category"] = cat if cat in CATEGORIES else "wildcard"
                    p["theme"] = clean_text(x.get("rotating_theme"), 80) if p["category"] == "rotating" else ""
                    p["long"] = bool(x.get("long_read", p["long"]))
                    p["hook"] = clean_text(x.get("hook"), 500) or p["hook"]
                    p["source"] = f"gemini:{model}"
                    picks.append(p)
                log(f"  Gemini picked {len(picks)} articles")
                if picks:
                    return picks
            except Exception as e:
                log(f"  failed: {e}")
            time.sleep(10 * attempt)
    return []


# ── curator 2 (backup): balanced picks from the pool without AI ───────────────
def from_backup(pool):
    log("Using the backup picker…")
    by_cat = {}
    for p in pool:
        by_cat.setdefault(p["category"], []).append(dict(p, source="backup"))
    pref = ["islamic", "rotating", "weird_history", "fraud_investigation", "science_tech",
            "weird_history", "fraud_investigation", "culture_ideas", "wildcard", "science_tech",
            "weird_history", "fraud_investigation"]
    items = []
    while any(by_cat.values()):
        for cat in pref + sorted(by_cat):
            if by_cat.get(cat):
                items.append(by_cat[cat].pop())
    return items


# ── selection ─────────────────────────────────────────────────────────────────
def select(cands, history):
    """Verify links and pick 10 short + 2 long, with ≥1 islamic and ≥1 rotating if possible."""
    good, per_pub = [], {}
    for c in cands:
        if len(good) >= 16:
            break
        if is_repeat(c, history, good) or per_pub.get(c["publisher"], 0) >= 2:
            continue
        final = verify(c["url"], c["title"], c["trusted"])
        if not final:
            for alt in c["alts"][:3]:  # Google found a page on the same site — try it
                final = verify(alt, c["title"], True)
                if final:
                    break
        if not final:
            continue
        c["url"], c["norm"] = final, dedup_key(final)
        if is_repeat(c, history, good):
            continue
        log(f"   ✓ {c['title'][:70]} — {c['publisher']}")
        good.append(c)
        per_pub[c["publisher"]] = per_pub.get(c["publisher"], 0) + 1

    longs = [c for c in good if c["long"]][:LONG_N]
    shorts = [c for c in good if not c["long"]]  # spare long reads are dropped, so it stays exactly 2
    picked = []
    for need in ("islamic", "rotating"):
        if not any(c["category"] == need for c in longs + picked):
            m = next((c for c in shorts if c["category"] == need and c not in picked), None)
            if m:
                picked.append(m)
    for c in shorts:
        if len(picked) >= SHORT_N:
            break
        if c not in picked:
            picked.append(c)
    return picked[:SHORT_N] + longs


# ── article images ───────────────────────────────────────────────────────────
class ImageMetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image = ""

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta" or self.image:
            return
        a = {k.lower(): (v or "") for k, v in attrs if k}
        if a.get("property", "").lower() in ("og:image", "og:image:url") or a.get("name", "").lower() == "twitter:image":
            self.image = html.unescape(a.get("content", "").strip())


def article_image(url):
    """Best-effort featured image. Failures never prevent publishing."""
    try:
        r = session.get(url, timeout=15, stream=True, allow_redirects=True)
        try:
            if r.status_code >= 400 or "html" not in r.headers.get("Content-Type", "").lower():
                return ""
            body = r.raw.read(300_000, decode_content=True).decode(r.encoding or "utf-8", "ignore")
            parser = ImageMetaParser()
            parser.feed(body)
            candidate = urljoin(r.url, parser.image) if parser.image else ""
            parsed = urlsplit(candidate)
            if parsed.scheme in ("https", "http") and parsed.hostname:
                return candidate
        finally:
            r.close()
    except Exception as e:
        log(f"   image unavailable for {domain(url)} ({type(e).__name__})")
    return ""


# ── feed ──────────────────────────────────────────────────────────────────────
CAT_LABEL = {"weird_history": "Weird history", "fraud_investigation": "Frauds & investigations",
             "science_tech": "Science & tech", "culture_ideas": "Culture & ideas",
             "islamic": "Islamic history & culture", "wildcard": "Wildcard", "rotating": "Special interest"}


def write_feed(history):
    cutoff = (dt.datetime.now(TZ) - dt.timedelta(days=KEEP_DAYS)).isoformat()
    items = [h for h in history if h["sent"] >= cutoff]
    ATOM = "http://www.w3.org/2005/Atom"
    ET.register_namespace("atom", ATOM)
    MEDIA = "http://search.yahoo.com/mrss/"
    ET.register_namespace("media", MEDIA)
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = FEED_TITLE
    ET.SubElement(ch, "link").text = FEED_LINK
    # WebSub: lets Inoreader & co. get new batches pushed instantly instead of polling.
    ET.SubElement(ch, f"{{{ATOM}}}link", rel="self", type="application/rss+xml", href=FEED_LINK.rstrip("/") + "/feed.xml")
    ET.SubElement(ch, f"{{{ATOM}}}link", rel="hub", href="https://pubsubhubbub.appspot.com/")
    ET.SubElement(ch, "description").text = "Daily Reads: Gemini Picks and ChatGPT Picks."
    ET.SubElement(ch, "language").text = "en"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(dt.datetime.now(TZ))
    ET.SubElement(ch, "ttl").text = "30"
    for h in sorted(items, key=lambda h: h["sent"], reverse=True):
        it = ET.SubElement(ch, "item")
        src = h.get("source", "")
        curator = "ChatGPT" if src == "chatgpt" else ("Backup" if src == "backup" else "Gemini")
        label = "[Long read] " if h["long"] else ""
        ET.SubElement(it, "title").text = f"[{curator}] {label}{h['title']}"
        ET.SubElement(it, "link").text = h["url"]
        ET.SubElement(it, "guid", isPermaLink="false").text = h["id"]
        ET.SubElement(it, "pubDate").text = format_datetime(dt.datetime.fromisoformat(h["sent"]))
        ET.SubElement(it, "category").text = f"{curator} Picks"
        ET.SubElement(it, "category").text = (h.get("theme") or CAT_LABEL.get(h["category"], "Wildcard"))
        meta = " · ".join(x for x in (h["publisher"], h.get("date")) if x)
        # ElementTree escapes everything, so untrusted text can't inject markup into the feed.
        description = f"{h['hook']}\n\n— {curator} Picks · {meta}".strip()
        image = h.get("image", "")
        if image:
            # Media RSS thumbnails are recognised by many readers.
            ET.SubElement(it, f"{{{MEDIA}}}thumbnail", url=image)
            ET.SubElement(it, f"{{{MEDIA}}}content", url=image, medium="image")
            # HTML fallback for readers that derive list thumbnails from descriptions.
            safe_image = html.escape(image, quote=True)
            description = f'<p><img src="{safe_image}" alt="" /></p><p>{html.escape(description)}</p>'
        ET.SubElement(it, "description").text = description
    FEED.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(rss).write(FEED, encoding="utf-8", xml_declaration=True)
    (FEED.parent / "index.html").write_text(
        f'<!doctype html><meta charset="utf-8"><title>{html.escape(FEED_TITLE)}</title>'
        f'<p>Subscribe in your reader: <a href="feed.xml">feed.xml</a></p>')



CHATGPT_PROCESSED = ROOT / "state" / "chatgpt_processed.json"
CHATGPT_MAX_AGE_DAYS = 2   # batches older than this are left alone (prevents surprise backfills)
ROTATING_THEMES = ("Hidden Worlds & Forgotten Places", "How the World Really Works",
                   "Unsolved Mysteries & Historical Puzzles", "Extraordinary People & Unusual Lives",
                   "Nature's Strangest Stories", "Food Mysteries & Culinary History")


def load_processed():
    try:
        return json.loads(CHATGPT_PROCESSED.read_text())
    except (OSError, ValueError):
        return {}


def known_publisher_domains():
    """Publications we already trust (sources.txt) — used only when a site blocks automated checks."""
    out = set()
    for _, _, url in read_sources():
        out.add(domain(url))
    return out


def slug_matches(url, title):
    """The article address itself names the story (e.g. /the-jumping-frenchmen-of-maine)."""
    slug = re.sub(r"[^a-z0-9]+", " ", urlsplit(url).path.lower())
    return len({w for w in slug.split() if len(w) > 3} & words(title)) >= 2


def import_chatgpt(history, processed):
    """Import dated ChatGPT batches from state/chatgpt_picks.json.

    - Each batch date is handled once (recorded in state/chatgpt_processed.json), so repeated runs,
      button presses or pushes never republish it.
    - Structure is checked per batch (12 articles, 2 long, 1 Islamic, 1 rotating with a valid theme);
      a malformed batch is reported and left unprocessed so a corrected version can be pushed.
    - Articles are checked one by one: a single bad link or repeat no longer sinks the other eleven.
      Every rejection is logged with its reason.
    """
    if not CHATGPT_INBOX.exists():
        log("ChatGPT: no inbox file (state/chatgpt_picks.json).")
        return []
    try:
        raw = json.loads(CHATGPT_INBOX.read_text())
        if not isinstance(raw, list):
            raise ValueError("expected a JSON array")
    except (ValueError, OSError) as e:
        log(f"ChatGPT: inbox unreadable — {e}")
        return []

    today = dt.datetime.now(TZ).date()
    groups = {}
    for x in raw:
        if isinstance(x, dict) and isinstance(x.get("batch_date"), str):
            groups.setdefault(x["batch_date"], []).append(x)
        else:
            log("ChatGPT: entry without a batch_date ignored")
    if str(today) not in groups:
        log(f"ChatGPT: no batch for today ({today}) in the inbox yet.")

    trusted_domains = None
    accepted = []
    for date, entries in sorted(groups.items()):
        if date in processed:
            continue                                    # already handled on an earlier run
        try:
            day = dt.date.fromisoformat(date)
        except ValueError:
            log(f"ChatGPT batch {date}: invalid date, ignored")
            continue
        if day > today:
            log(f"ChatGPT batch {date}: dated in the future, ignored")
            continue
        if (today - day).days > CHATGPT_MAX_AGE_DAYS:
            log(f"ChatGPT batch {date}: older than {CHATGPT_MAX_AGE_DAYS} days, ignored")
            continue
        problems = []
        if len(entries) != 12:
            problems.append(f"{len(entries)} articles instead of 12")
        if sum(x.get("long") is True for x in entries) != 2:
            problems.append("needs exactly 2 long reads")
        if sum(x.get("category") == "islamic" for x in entries) != 1:
            problems.append("needs exactly 1 Islamic article")
        rot = [x for x in entries if x.get("category") == "rotating"]
        if len(rot) != 1:
            problems.append("needs exactly 1 rotating-theme article")
        elif rot[0].get("theme") not in ROTATING_THEMES:
            problems.append(f"unknown rotating theme {rot[0].get('theme')!r}")
        if problems:
            log(f"ChatGPT batch {date}: not imported — " + "; ".join(problems) + ". Fix and push again.")
            continue

        log(f"ChatGPT batch {date}: checking 12 articles…")
        batch, rejected = [], []
        for x in entries:
            title = clean_text(x.get("title"), 300)
            url = normalize_url(str(x.get("url", "")))

            def reject(reason):
                rejected.append({"title": title or "(no title)", "url": x.get("url", ""), "reason": reason})
                log(f"   ✗ ChatGPT: {title[:70]!r} — {reason}")

            if not url or not title or not x.get("hook") or x.get("category") not in CATEGORIES:
                reject("missing title, link, hook or valid category")
                continue
            if domain(url) in BLOCKED_DOMAINS:
                reject("blocked publisher")
                continue
            p = {"title": title, "url": url, "norm": dedup_key(url),
                 "publisher": clean_text(x.get("publisher") or domain(url), 100),
                 "date": clean_text(x.get("date"), 20), "category": x["category"],
                 "theme": clean_text(x.get("theme"), 100) if x["category"] == "rotating" else "",
                 "long": x.get("long") is True, "hook": clean_text(x["hook"], 500), "source": "chatgpt"}
            if is_repeat(p, history + accepted, batch):
                dup = next((h for h in history + accepted + batch if h.get("norm") == p["norm"]), None)
                reject(f"already in the feed ({'same link' if dup else 'near-identical headline'})")
                continue
            final = verify(url, title, trusted=False)
            if not final and VERIFY["blocked"]:
                # Site blocks robots. Accept only a known publication whose address names this story.
                trusted_domains = trusted_domains or known_publisher_domains()
                f = VERIFY.get("final") or url
                if domain(f) in trusted_domains and slug_matches(f, title):
                    final = f
                    log(f"   ~ ChatGPT: {title[:70]!r} — site blocks checks; accepted (known publisher, link names the story)")
            if not final:
                reject(VERIFY["reason"] or "link check failed")
                continue
            p["url"], p["norm"] = final, dedup_key(final)
            if is_repeat(p, history + accepted, batch):
                reject("already in the feed (same article after redirect)")
                continue
            batch.append(p)
            log(f"   ✓ ChatGPT: {title[:70]} — {p['publisher']}")
        processed[date] = {"checked": dt.datetime.now(TZ).isoformat(timespec="seconds"),
                           "published": len(batch), "rejected": rejected}
        log(f"ChatGPT batch {date}: {len(batch)} published, {len(rejected)} rejected")
        if batch and sum(p["long"] for p in batch) < 2:
            log(f"   note: only {sum(p['long'] for p in batch)} long read(s) survived the checks")
        accepted.extend(batch)
    return accepted


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    mode = os.environ.get("RUN_MODE", "full")   # "full" = Gemini + ChatGPT, "chatgpt" = ChatGPT inbox only
    log(f"Mode: {mode}")
    history = load_history()
    processed = load_processed()

    # ChatGPT's preselected batch goes first, so Gemini can't crowd it out.
    chatgpt = import_chatgpt(history, processed)

    picks = []
    if mode == "full":
        taken = history + chatgpt                 # Gemini must pick different articles
        pool = collect_pool(taken)
        picks = select(from_gemini(pool, taken), taken)
        if len(picks) < SHORT_N + LONG_N:
            log(f"Gemini gave {len(picks)} usable articles — topping up from the backup list.")
            backup = select(from_backup(pool), taken + picks)
            need_long = LONG_N - sum(p["long"] for p in picks)
            longs = [b for b in backup if b["long"]][:max(need_long, 0)]
            rest = [b for b in backup if b not in longs]
            picks += longs + rest[:SHORT_N + LONG_N - len(picks) - len(longs)]

    CHATGPT_PROCESSED.write_text(json.dumps(processed, indent=1, ensure_ascii=False))
    new = picks + chatgpt
    if not new:
        if mode == "chatgpt":
            log("Nothing new from ChatGPT — feed unchanged.")
            save_log()
            return
        log("No articles could be published today — keeping the existing feed.")
        save_log()
        sys.exit(1)

    now = dt.datetime.now(TZ)
    for i, p in enumerate(new):
        sent = (now - dt.timedelta(seconds=i)).isoformat(timespec="seconds")
        p["image"] = article_image(p["url"])
        history.append({k: p.get(k, "") for k in ("title", "url", "norm", "publisher", "date",
                                                  "category", "theme", "long", "hook", "source", "image")}
                       | {"id": f"{now:%Y%m%d}-{i:02d}-{abs(hash(p['norm'])) % 10**8}", "sent": sent})
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(history[-3000:], indent=1, ensure_ascii=False))
    write_feed(history)
    log(f"Published {len(new)} articles: {len(picks)} Gemini/backup + {len(chatgpt)} ChatGPT "
        f"({sum(p['long'] for p in new)} long reads).")
    save_log()


if __name__ == "__main__":
    main()
