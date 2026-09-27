"""collect_commlinks.py - keep the RSI comm-link archive current and fetch article bodies.

Two jobs, both incremental and safe to re-run (the daily GitHub Action runs them; you can too):

  1. NEW COMM-LINKS. Walk the comm-link listing newest-first until a whole page is already known, and add the new
     ones to chronology/commlink_index.json (raw listing) and chronology/web_records.json (dated records).
  2. BODIES. Fetch the full article text for comm-links that do not have it yet, into commlinks/<id>.txt, a bounded
     number per run (--max-bodies) so the backfill of ~5,000 articles spreads over days instead of hammering RSI.
     Priority: Monthly Reports, then engineering / Roadmap Roundups / Q&As, then other transmissions, then
     community and lore. Newest first inside each tier. Results are remembered in chronology/commlink_bodies.json;
     a failure or an empty body (a video-only post, or RSI changed the page) is retried on the next runs, up to
     three times, then left alone.

    python tools/collect_commlinks.py                     # both jobs, default budget
    python tools/collect_commlinks.py --max-bodies 50     # smaller run
    python tools/collect_commlinks.py --probe             # fetch 1 listing page + 1 body, print, write nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rsi  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "chronology" / "commlink_index.json"
RECORDS = ROOT / "chronology" / "web_records.json"
STATE = ROOT / "chronology" / "commlink_bodies.json"
BODIES = ROOT / "commlinks"
MAX_TRIES = 3
MONTHLY = re.compile(r"\bmonthly (studio )?report\b", re.I)


def load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def commlink_id(url: str) -> str:
    m = re.search(r"/comm-link/[\w-]+/(\d+)-", url)
    return m.group(1) if m else ""


def listing_page(client: rsi.Client, page: int) -> list[dict]:
    j = client.json("/api/hub/getCommlinkItems",
                    {"channel": "", "series": "", "type": "", "text": "", "sort": "publish_new", "page": page})
    if not j.get("success"):
        raise RuntimeError(f"getCommlinkItems page {page}: {j.get('code')} {j.get('msg')}")
    return rsi.parse_commlink_cards(j.get("data") or "")


def update_listing(client: rsi.Client, today: dt.date, max_pages: int = 40) -> int:
    index = load(INDEX, [])
    records = load(RECORDS, [])
    known = {x["id"] for x in index}
    new_raw, new_records = [], []
    for page in range(1, max_pages + 1):
        cards = listing_page(client, page)
        if not cards:
            break
        fresh = [c for c in cards if c["id"] not in known]
        for c in fresh:
            known.add(c["id"])
            date, precision = rsi.date_from_age(c["age_text"], today)
            new_raw.append(dict(c, _page=page, _seen=today.isoformat()))
            new_records.append({"source": "rsi-comm-link", "type": c["type"], "title": c["title"], "date": date,
                                "date_precision": precision, "date_source": "age_text", "url": c["url"],
                                "summary": c["teaser"]})
        if not fresh:
            break                       # a whole page we already have: everything older is known too
    if new_raw:
        save(INDEX, new_raw + index)    # the raw listing is kept newest-first, as RSI serves it
        records.extend(new_records)
        records.sort(key=lambda r: r.get("date") or "")       # stable: existing order kept within a day
        save(RECORDS, records)
    print(f"listing: {len(new_raw)} new comm-link(s)")
    for r in new_records:
        print(f"  + {r['date']}  {r['title']}")
    return len(new_raw)


def tier(rec: dict) -> int:
    t, ty = rec.get("title", ""), rec.get("type", "")
    if MONTHLY.search(t):
        return 0
    if ty == "engineering" or re.search(r"roadmap roundup|\bq&a\b|patch notes|alpha \d", t, re.I):
        return 1
    if ty == "transmission":
        return 2
    if ty == "citizens":
        return 3
    return 4                            # spectrum-dispatch, serialized-fiction and the rest


def fetch_bodies(client: rsi.Client, max_bodies: int) -> dict:
    records = load(RECORDS, [])
    state = load(STATE, {})
    todo = []
    for r in records:
        cid = commlink_id(r["url"])
        s = state.get(cid) or {}
        if not cid or s.get("s") == "ok" or s.get("n", 0) >= MAX_TRIES:
            continue
        todo.append((tier(r), r.get("date") or "", cid, r))
    todo.sort(key=lambda x: x[1], reverse=True)     # newest first...
    todo.sort(key=lambda x: x[0])                   # ...within each tier (sort is stable)
    stats = {"ok": 0, "empty": 0, "err": 0, "remaining": max(0, len(todo) - max_bodies)}
    BODIES.mkdir(exist_ok=True)
    for _tier, _date, cid, r in todo[:max_bodies]:
        s = state.get(cid) or {"n": 0}
        try:
            text = rsi.commlink_body(client, r["url"])
        except Exception as exc:        # one bad article must not stop the run; recorded and retried next time
            s.update(s="err", n=s.get("n", 0) + 1, e=f"{type(exc).__name__}: {exc}"[:200])
            state[cid] = s
            stats["err"] += 1
            print(f"  ! {cid} {r['title'][:60]}: {s['e']}")
            continue
        words = len(text.split())
        if words < 5:
            s.update(s="empty", n=s.get("n", 0) + 1, w=words)
            stats["empty"] += 1
        else:
            (BODIES / f"{cid}.txt").write_text(text + "\n", encoding="utf-8")
            s = {"s": "ok", "w": words}
            stats["ok"] += 1
        state[cid] = s
        if (stats["ok"] + stats["empty"] + stats["err"]) % 50 == 0:
            save(STATE, state)          # checkpoint: a cancelled run keeps what it fetched
    save(STATE, state)
    print(f"bodies: {stats['ok']} fetched, {stats['empty']} empty, {stats['err']} failed, "
          f"{stats['remaining']} still to do")
    return stats


def probe(client: rsi.Client) -> int:
    cards = listing_page(client, 1)
    print(f"listing page 1: {len(cards)} cards")
    for c in cards[:5]:
        print(f"  {c['id']}  {c['age_text']:>12}  {c['title']}")
    mr = next((c for c in cards if MONTHLY.search(c["title"])), cards[0] if cards else None)
    if mr:
        text = rsi.commlink_body(client, mr["url"])
        print(f"\nbody of {mr['title']!r}: {len(text.split())} words\n" + "\n".join(text.splitlines()[:12]))
    return 0 if cards else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-bodies", type=int, default=800)
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--skip-listing", action="store_true")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args(argv)
    client = rsi.Client(delay=a.delay)
    if a.probe:
        return probe(client)
    if not a.skip_listing:
        update_listing(client, dt.date.today())
    if a.max_bodies > 0:
        fetch_bodies(client, a.max_bodies)
    print(f"{client.requests} request(s) to RSI")
    return 0


if __name__ == "__main__":
    sys.exit(main())
