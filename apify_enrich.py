#!/usr/bin/env python3
"""Enrich yc_founders.json with founder emails via the Apify actor snipercoder/email-finder-by-name-and-domain.

Needs an Apify token: env APIFY_TOKEN, or a line `APIFY_TOKEN=...` in .env next to this script.
Usage: python3 apify_enrich.py [--limit 5] [--chunk 50] [--max-charge 1]
Writes <out>.json / .csv (adds `apify_email` per founder) and <out>_apify_raw.json (default out: yc_founders_enriched).
"""
import argparse, csv, json, os, pathlib, re, sys, time, urllib.parse, urllib.request

ACTOR = "snipercoder~email-finder-by-name-and-domain"
API = "https://api.apify.com/v2"
HERE = pathlib.Path(__file__).parent


def token():
    if os.environ.get("APIFY_TOKEN"):
        return os.environ["APIFY_TOKEN"]
    env = HERE / ".env"
    if env.exists():
        m = re.search(r"^APIFY_TOKEN=(\S+)", env.read_text(), re.M)
        if m:
            return m.group(1).strip("'\"")
    sys.exit("Set APIFY_TOKEN (env var or .env file)")


def api(method, path, tok, body=None):
    req = urllib.request.Request(f"{API}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def domain_of(website):
    host = urllib.parse.urlparse(website if "//" in (website or "") else f"https://{website}").hostname or ""
    return host.removeprefix("www.").lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="yc_founders.json")
    ap.add_argument("--out", default="yc_founders_enriched")
    ap.add_argument("--limit", type=int, default=0, help="only send the first N founders (for testing)")
    ap.add_argument("--max-charge", type=float, default=1.0, help="hard USD cap per Apify run")
    ap.add_argument("--chunk", type=int, default=50, help="founders per Apify run")
    a = ap.parse_args()
    tok = token()

    data = json.load(open(HERE / a.input))
    pairs = list(dict.fromkeys((f["name"], domain_of(c["website"])) for c in data for f in c["founders"]
                               if f.get("name") and domain_of(c["website"])))
    if a.limit:
        pairs = pairs[: a.limit]
    print(f"Sending {len(pairs)} name+domain pairs to {ACTOR} (cap ${a.max_charge})", file=sys.stderr)

    # Large single runs crash the actor, so send chunks and keep results as each finishes
    items = []
    for i in range(0, len(pairs), a.chunk):
        chunk = pairs[i : i + a.chunk]
        body = {"names": [n for n, _ in chunk], "domains": [d for _, d in chunk]}
        run = api("POST", f"/acts/{ACTOR}/runs?maxTotalChargeUsd={a.max_charge}&memory=1024", tok, body)["data"]
        while run["status"] in ("READY", "RUNNING"):
            time.sleep(10)
            run = api("GET", f"/actor-runs/{run['id']}", tok)["data"]
        got = api("GET", f"/datasets/{run['defaultDatasetId']}/items?clean=true&format=json", tok)
        items += got
        print(f"[{i + len(chunk)}/{len(pairs)}] run {run['id']} {run['status']}: {len(got)} items", file=sys.stderr)
        json.dump(items, open(HERE / f"{a.out}_apify_raw.json", "w"), indent=2, ensure_ascii=False)

    # Output field names aren't documented, so key each item by any name/domain values it echoes back
    by_person, status = {}, {}
    for it in items:
        vals = {str(v).strip().lower() for v in it.values() if isinstance(v, (str, int))}
        emails = {e.lower() for e in re.findall(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", json.dumps(it), re.I)}
        for n, d in pairs:
            if n.lower() in vals and d in vals:
                by_person.setdefault((n, d), set()).update(e for e in emails if e.endswith("@" + d))
                status[(n, d)] = it.get("Validation", "")

    hits = 0
    for c in data:
        for f in c["founders"]:
            key = (f["name"], domain_of(c["website"]))
            f["apify_email"] = "; ".join(sorted(by_person.get(key, [])))
            f["apify_email_status"] = status.get(key, "") if f["apify_email"] else ""
            hits += bool(f["apify_email"])
    print(f"{len(items)} items returned, emails matched for {hits} founders", file=sys.stderr)

    out = HERE / a.out
    json.dump(data, open(f"{out}.json", "w"), indent=2, ensure_ascii=False)
    with open(f"{out}.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "batch", "website", "yc_url", "one_liner", "description", "company_linkedin", "company_twitter",
                    "site_emails", "founder", "founder_title", "founder_linkedin", "founder_twitter", "apify_email", "apify_email_status",
                    "founder_emails_found", "founder_email_guesses"])
        for c in data:
            for f in c["founders"]:
                w.writerow([c["name"], c["batch"], c["website"], c["yc_url"], c["one_liner"], c["description"], c["linkedin"],
                            c["twitter"], "; ".join(c["site_emails"]), f["name"], f["title"], f["linkedin"], f["twitter"],
                            f["apify_email"], f["apify_email_status"], "; ".join(f["emails_found"]), "; ".join(f["email_guesses"])])
    print(f"Wrote {out}.json and {out}.csv", file=sys.stderr)


if __name__ == "__main__":
    main()
