"""Vercel serverless function: YC batches, companies and founders. Stdlib only, no API key.

GET /api/yc?action=batches
GET /api/yc?action=companies&batch=Winter%202024
GET /api/yc?action=founders&slugs=airbnb,stripe        (max 10 slugs per call)

Founder names only live on each company's ycombinator.com page, which browsers can't fetch
cross-origin, so this runs server-side. Emails: addresses on the company site plus pattern guesses.
Optional verified lookup (Apify) runs in the browser, so visitors' tokens never reach this server.
"""
import concurrent.futures as cf, html, json, re, socket, ssl, unicodedata, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
BAD_EMAIL = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js)$|sentry|wixpress|example\.|@2x|u00|domain\.com|email\.com", re.I)
SLUG_RE = re.compile(r"^[a-z0-9-]{1,100}$")
BATCH_RE = re.compile(r"^(Winter|Spring|Summer|Fall) \d{4}$|^[WSFX]\d{2}$|^IK12$|^Unspecified$")
MAX_SLUGS = 10
CTX = ssl.create_default_context()
SEASON = {"Winter": 1, "Spring": 2, "Summer": 3, "Fall": 4}
_algolia = None  # (app, key), cached per warm instance
_pool = cf.ThreadPoolExecutor(32)  # site checks; outlives a request so a slow site can be abandoned
SITE_DEADLINE = 4  # seconds per company before giving up on its website


def get(url, timeout=8, data=None, headers=None):
    # ponytail: one attempt, short timeout; the CLI (yc_scraper.py) retries, a web request can't wait
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.read(2_000_000).decode("utf-8", "ignore")
    except Exception:
        return None


def algolia(params):
    global _algolia
    if not _algolia:
        m = re.search(r'window\.AlgoliaOpts = \{"app":"(\w+)","key":"([^"]+)"', get("https://www.ycombinator.com/companies") or "")
        if not m:
            raise RuntimeError("Could not read YC's company search")
        _algolia = m.group(1), m.group(2)
    app, key = _algolia
    body = json.dumps({"params": urllib.parse.urlencode(params)}).encode()
    for host in (f"{app}-dsn.algolia.net", f"{app}-1.algolianet.com", f"{app}-2.algolianet.com"):  # Algolia's fallback hosts
        res = get(f"https://{host}/1/indexes/YCCompany_production/query", data=body,
                  headers={"X-Algolia-Application-Id": app, "X-Algolia-API-Key": key})
        if res is not None:
            return json.loads(res)
    _algolia = None  # key may have rotated
    raise RuntimeError("YC company search failed")


def batch_key(b):
    p = b.split()
    return (int(p[1]), SEASON.get(p[0], 0)) if len(p) == 2 and p[1].isdigit() else (0, 0)


def batches():
    facets = algolia({"hitsPerPage": 0, "facets": json.dumps(["batch"]), "maxValuesPerFacet": 1000})["facets"]["batch"]
    return [{"batch": b, "count": n} for b, n in sorted(facets.items(), key=lambda x: batch_key(x[0]), reverse=True)]


def companies(batch):
    hits, page = [], 0
    while True:
        res = algolia({"hitsPerPage": 1000, "page": page, "facetFilters": json.dumps([[f"batch:{batch}"]])})
        hits += res["hits"]
        page += 1
        if page >= res["nbPages"]:
            break
    return [{"name": h["name"], "slug": h["slug"], "batch": h.get("batch"), "website": h.get("website") or "",
             "one_liner": h.get("one_liner") or "", "industry": h.get("subindustry") or "", "team_size": h.get("team_size"),
             "launched_at": h.get("launched_at") or 0} for h in hits]


def domain_of(website):
    host = urllib.parse.urlparse(website if "//" in (website or "") else f"https://{website}").hostname or ""
    return host.removeprefix("www.").lower()


def ascii_name(s):
    return re.sub(r"[^a-z ]", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()).split()


TECH_PATTERNS = [
    ("Next.js", re.compile(r"/_next/static|__NEXT_DATA__", re.I)),
    ("React", re.compile(r"react(?:\.production\.min)?\.js|_reactRootContainer|react-dom", re.I)),
    ("Tailwind", re.compile(r"cdn\.tailwindcss\.com|class=[\"'][^\"']*?\b(?:flex|grid|p-\d|m-\d|text-)\b", re.I)),
    ("Supabase", re.compile(r"supabase\.co", re.I)),
    ("Stripe", re.compile(r"js\.stripe\.com", re.I)),
    ("FastAPI", re.compile(r"fastapi", re.I)),
    ("PostHog", re.compile(r"app\.posthog\.com|posthog-js", re.I)),
    ("Segment", re.compile(r"cdn\.segment\.com", re.I)),
    ("Cloudflare", re.compile(r"cloudflare", re.I)),
    ("Vue", re.compile(r"/vue(?:\.min)?\.js|data-v-[a-f0-9]", re.I)),
    ("Nuxt", re.compile(r"/_nuxt/", re.I)),
    ("Svelte", re.compile(r"svelte-", re.I)),
    ("Webflow", re.compile(r"assets\.webflow\.com|w-nav", re.I)),
]

CAREERS_RE = re.compile(
    r'href=["\'](https?://(?:boards\.greenhouse\.io|jobs\.lever\.co|jobs\.ashbyhq\.com|apply\.workable\.com)/[^\s"\'<>]+|/[a-z0-9_-]*(?:careers|jobs|openings|positions)[a-z0-9_-]*)["\']',
    re.I
)


def site_intel(website, domain):
    found = set()
    tech = set()
    careers = ""
    for path in ("", "/contact"):
        body = get(website.rstrip("/") + path, timeout=3)
        if not body:
            continue
        for e in EMAIL_RE.findall(urllib.parse.unquote(html.unescape(body))):
            e = e.lower().strip(".")
            host = e.split("@")[1]
            if not BAD_EMAIL.search(e) and (host == domain or host.endswith("." + domain)):
                found.add(e)
        for name, pattern in TECH_PATTERNS:
            if pattern.search(body):
                tech.add(name)
        if not careers:
            cm = CAREERS_RE.search(body)
            if cm:
                u = cm.group(1)
                careers = u if u.startswith("http") else (website.rstrip("/") + u)
    return sorted(found), sorted(tech), careers


def guesses(full_name, domain):
    parts = ascii_name(full_name)
    if not parts or not domain:
        return []
    first, last = parts[0], parts[-1] if len(parts) > 1 else ""
    return [f"{first}@{domain}"] + ([f"{first}.{last}@{domain}", f"{first[0]}{last}@{domain}", f"{first}{last}@{domain}"] if last else [])


def resolves(domain):
    try:
        socket.getaddrinfo(domain, None)
        return True
    except Exception:
        return False


def founders(slug):
    page = get(f"https://www.ycombinator.com/companies/{slug}")
    m = re.search(r'data-page="([^"]*)"', page or "")
    if not m:
        return {"slug": slug, "error": "Couldn't load this company's YC page"}
    c = json.loads(html.unescape(m.group(1)))["props"]["company"]
    website = c.get("website") or ""  # always from YC, never from the client
    domain = domain_of(website)
    try:
        emails, tech_stack, careers_url = _pool.submit(site_intel, website, domain).result(timeout=SITE_DEADLINE) if domain else ([], [], "")
    except cf.TimeoutError:
        emails, tech_stack, careers_url = [], [], ""
    dns = resolves(domain) if domain else False
    out = []
    for f in c.get("founders", []):
        name = f.get("full_name") or ""
        first = (ascii_name(name) or [""])[0]
        out.append({"name": name, "title": f.get("title") or "", "linkedin": f.get("linkedin_url") or "",
                    "twitter": f.get("twitter_url") or "",
                    "emails_found": [e for e in emails if first and e.split("@")[0].startswith(first)],
                    "email_guesses": guesses(name, domain) if dns else []})
    return {"slug": slug, "website": website, "domain": domain, "linkedin": c.get("linkedin_url") or "",
            "twitter": c.get("twitter_url") or "", "site_emails": emails, "tech_stack": tech_stack,
            "careers_url": careers_url, "founders": out}


def route(query):
    q = urllib.parse.parse_qs(query)
    action = (q.get("action") or [""])[0]
    if action == "batches":
        return 200, batches(), 86400
    if action == "companies":
        batch = (q.get("batch") or [""])[0]
        if not BATCH_RE.match(batch):
            return 400, {"error": "Invalid batch"}, 0
        return 200, companies(batch), 3600
    if action == "founders":
        slugs = [s for s in (q.get("slugs") or [""])[0].split(",") if s]
        if not slugs or len(slugs) > MAX_SLUGS or not all(SLUG_RE.match(s) for s in slugs):
            return 400, {"error": f"Pass 1-{MAX_SLUGS} valid slugs"}, 0
        with cf.ThreadPoolExecutor(len(slugs)) as ex:
            return 200, list(ex.map(founders, slugs)), 86400
    return 400, {"error": "Unknown action"}, 0


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            status, body, cache = route(urllib.parse.urlparse(self.path).query)
        except Exception as e:
            status, body, cache = 502, {"error": str(e) or "Upstream error"}, 0
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", f"public, s-maxage={cache}, stale-while-revalidate=86400")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()
