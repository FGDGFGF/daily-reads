#!/usr/bin/env python3
"""Daily Reads — curates 12 articles a day and publishes them as an RSS feed.

Curator chain: Gemini (main model) -> Gemini (lite model) -> backup reading list (sources.txt).
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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "state" / "history.json"
FEED = ROOT / "docs" / "feed.xml"
PROMPT = ROOT / "prompt.txt"
SOURCES = ROOT / "sources.txt"

FEED_TITLE = os.environ.get("FEED_TITLE", "Daily Reads")
FEED_LINK = os.environ.get("FEED_LINK", "https://github.com")  # set by the workflow to the Pages URL
TZ = dt.timezone(dt.timedelta(hours=4))  # Mauritius
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "60"))
SHORT_N, LONG_N = 10, 2
MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-flash-latest,gemini-flash-lite-latest").split(",") if m.strip()]
BLOCKED_DOMAINS = {"statnews.com"}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_src|cmpid|smid|ito$|soc_)", re.I)
CATEGORIES = {"weird_history", "fraud_investigation", "science_tech", "culture_ideas",
              "islamic", "wildcard", "rotating"}

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "en;q=0.9"})


def log(*a):
    print(*a, flush=True)


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
    u = item["norm"]
    for h in list(history) + list(extra):
        if h.get("norm") == u or similar(h["title"], item["title"]) > 0.85:
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


def verify(url, claimed_title, trusted=False):
    """Returns the final canonical URL if the link really points at that article, else None."""
    try:
        r = session.get(url, timeout=20, allow_redirects=True, stream=True)
        body = r.raw.read(400_000, decode_content=True).decode(r.encoding or "utf-8", "ignore")
        r.close()
    except Exception as e:
        log(f"   ✗ unreachable {url} ({type(e).__name__})")
        return None
    final = normalize_url(r.url) or url
    if domain(final) in BLOCKED_DOMAINS:
        return None
    path = urlsplit(final).path
    if path in ("", "/") or len(path) < 6:
        log(f"   ✗ lands on a homepage {url}")
        return None
    if r.status_code in (401, 403, 429):
        # Bot-protected site: accept only if Google's search results pointed at it.
        if trusted:
            return final
        log(f"   ✗ blocked ({r.status_code}) and unconfirmed {url}")
        return None
    if r.status_code >= 400:
        log(f"   ✗ HTTP {r.status_code} {url}")
        return None
    t = page_title(body)
    if t and claimed_title:
        overlap = words(t) & words(claimed_title)
        if similar(t, claimed_title) < 0.35 and len(overlap) < 2:
            log(f"   ✗ page title doesn't match ({t[:60]!r}) {url}")
            return None
    return final


# ── curator 1: Gemini ─────────────────────────────────────────────────────────
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
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 1.0}}
    r = requests.post(url, params={"key": key}, json=body, timeout=240)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise RuntimeError("no JSON array in reply")
    items = json.loads(m.group(0))
    # Pages Google actually found (redirect links) — used to confirm/repair URLs.
    grounded = [c.get("web", {}) for c in cand.get("groundingMetadata", {}).get("groundingChunks", [])]
    return items, grounded


def resolve_grounded(grounded):
    out = []
    for g in grounded:
        uri = g.get("uri")
        if not uri:
            continue
        try:
            r = session.head(uri, timeout=15, allow_redirects=True)
            n = normalize_url(r.url)
            if n:
                out.append(n)
        except Exception:
            pass
    return out


def from_gemini(history):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        log("No GEMINI_API_KEY set — skipping Gemini.")
        return []
    prompt = build_prompt(history)
    for model in MODELS:
        for attempt in range(1, 4):
            try:
                log(f"Gemini {model}, attempt {attempt}…")
                raw, grounded = gemini(model, prompt, key)
                found = resolve_grounded(grounded)
                found_domains = {}
                for u in found:
                    found_domains.setdefault(domain(u), []).append(u)
                log(f"  got {len(raw)} candidates, {len(found)} search-confirmed pages")
                items = []
                for x in raw:
                    if not isinstance(x, dict):
                        continue
                    title = clean_text(x.get("title"), 300)
                    url = normalize_url(str(x.get("url", "")))
                    if not title or not url or domain(url) in BLOCKED_DOMAINS:
                        continue
                    cat = str(x.get("category", "wildcard")).strip()
                    items.append({
                        "title": title, "url": url, "norm": dedup_key(url),
                        "publisher": clean_text(x.get("publisher"), 120) or domain(url),
                        "date": clean_text(x.get("date"), 10),
                        "category": cat if cat in CATEGORIES else "wildcard",
                        "theme": clean_text(x.get("rotating_theme"), 80) if cat == "rotating" else "",
                        "long": bool(x.get("long_read")),
                        "hook": clean_text(x.get("hook"), 500),
                        "trusted": dedup_key(url) in {dedup_key(f) for f in found},
                        "alts": [u for u in found_domains.get(domain(url), []) if u != url],
                        "source": f"gemini:{model}",
                    })
                if items:
                    return items
            except Exception as e:
                log(f"  failed: {e}")
            time.sleep(10 * attempt)
    return []


# ── curator 2: backup reading list ────────────────────────────────────────────
def parse_feed(xml_text):
    out = []
    root = ET.fromstring(xml_text)
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        get = lambda name: next((c for c in el if c.tag.split("}")[-1] == name), None)
        t, l = get("title"), get("link")
        d = get("description")
        if d is None:
            d = get("summary")
        link = (l.get("href") if l is not None and l.get("href") else (l.text if l is not None else "")) or ""
        out.append({"title": clean_text(t.text if t is not None else ""),
                    "url": link.strip(),
                    "hook": clean_text(d.text if d is not None else "", 280)})
    return out


def from_backup(history):
    log("Using the backup reading list…")
    by_cat = {}
    for line in SOURCES.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            cat, long_, url = [p.strip() for p in line.split("|", 2)]
        except ValueError:
            continue
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
            entries = parse_feed(r.content)
        except Exception as e:
            log(f"  feed failed: {url} ({type(e).__name__})")
            continue
        pub = domain(url)
        for e in entries[:15]:
            n = normalize_url(e["url"]) if e["url"] else None
            if not n or not e["title"]:
                continue
            by_cat.setdefault(cat, []).append({
                "title": e["title"], "url": n, "norm": dedup_key(n), "publisher": pub, "date": "",
                "category": cat, "theme": "", "long": long_ == "yes",
                "hook": e["hook"], "trusted": True, "alts": [], "source": "backup"})
    items = []
    for cat, lst in by_cat.items():
        random.shuffle(lst)
        items.extend(lst)
    return items


# ── selection ─────────────────────────────────────────────────────────────────
def select(cands, history):
    """Verify links and pick 10 short + 2 long, with ≥1 islamic and ≥1 rotating if possible."""
    good, per_pub = [], {}
    order = (sorted(cands, key=lambda c: c["category"] not in ("islamic", "rotating"))
             if cands and cands[0]["source"] == "backup" else cands)
    for c in order:
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


# ── feed ──────────────────────────────────────────────────────────────────────
CAT_LABEL = {"weird_history": "Weird history", "fraud_investigation": "Frauds & investigations",
             "science_tech": "Science & tech", "culture_ideas": "Culture & ideas",
             "islamic": "Islamic history & culture", "wildcard": "Wildcard", "rotating": "Special interest"}


def write_feed(history):
    cutoff = (dt.datetime.now(TZ) - dt.timedelta(days=KEEP_DAYS)).isoformat()
    items = [h for h in history if h["sent"] >= cutoff]
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = FEED_TITLE
    ET.SubElement(ch, "link").text = FEED_LINK
    ET.SubElement(ch, "description").text = "12 hand-picked reads every morning."
    ET.SubElement(ch, "language").text = "en"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(dt.datetime.now(TZ))
    ET.SubElement(ch, "ttl").text = "180"
    for h in sorted(items, key=lambda h: h["sent"], reverse=True):
        it = ET.SubElement(ch, "item")
        label = "[Long read] " if h["long"] else ""
        ET.SubElement(it, "title").text = label + h["title"]
        ET.SubElement(it, "link").text = h["url"]
        ET.SubElement(it, "guid", isPermaLink="false").text = h["id"]
        ET.SubElement(it, "pubDate").text = format_datetime(dt.datetime.fromisoformat(h["sent"]))
        ET.SubElement(it, "category").text = (h.get("theme") or CAT_LABEL.get(h["category"], "Wildcard"))
        meta = " · ".join(x for x in (h["publisher"], h.get("date"),
                                       "backup pick" if h["source"] == "backup" else "") if x)
        # ElementTree escapes everything, so untrusted text can't inject markup into the feed.
        ET.SubElement(it, "description").text = f"{h['hook']}\n\n— {meta}".strip()
    FEED.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(rss).write(FEED, encoding="utf-8", xml_declaration=True)
    (FEED.parent / "index.html").write_text(
        f'<!doctype html><meta charset="utf-8"><title>{html.escape(FEED_TITLE)}</title>'
        f'<p>Subscribe in your reader: <a href="feed.xml">feed.xml</a></p>')


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    history = load_history()
    picks = select(from_gemini(history), history)
    if len(picks) < SHORT_N + LONG_N:
        log(f"Gemini gave {len(picks)} usable articles — topping up from the backup list.")
        backup = select(from_backup(history), history + picks)
        need_long = LONG_N - sum(p["long"] for p in picks)
        longs = [b for b in backup if b["long"]][:max(need_long, 0)]
        rest = [b for b in backup if b not in longs]
        picks += longs + rest[:SHORT_N + LONG_N - len(picks) - len(longs)]

    if not picks:
        log("No articles could be published today — keeping the existing feed.")
        sys.exit(1)

    now = dt.datetime.now(TZ)
    for i, p in enumerate(picks):
        sent = (now - dt.timedelta(seconds=i)).isoformat(timespec="seconds")
        history.append({k: p[k] for k in ("title", "url", "norm", "publisher", "date",
                                           "category", "theme", "long", "hook", "source")}
                       | {"id": f"{now:%Y%m%d}-{i:02d}-{abs(hash(p['norm'])) % 10**8}", "sent": sent})
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(history[-3000:], indent=1, ensure_ascii=False))
    write_feed(history)
    log(f"Published {len(picks)} articles ({sum(p['long'] for p in picks)} long reads).")


if __name__ == "__main__":
    main()
