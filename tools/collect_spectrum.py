"""collect_spectrum.py - archive CIG developer posts from the RSI Devtracker (Spectrum).

  1. NEW POSTS. Walk the Devtracker newest-first until a whole page is already known; add new posts to
     chronology/devtracker.json (author, forum category, thread, date, the public teaser and a link).
  2. BACKFILL. Walk further back a bounded number of pages per run (--backfill-pages), resuming where the last run
     stopped, until the Devtracker runs out (it goes back to late 2021, roughly 900 pages).
  3. BODIES. Fetch the full text of each tracked post from its public Spectrum thread into devposts/<id>.txt, a bounded
     number per run (--max-bodies). Posts in private forums (Focus Testing, Evocati...) answer "permission denied":
     for those we keep only the teaser the public Devtracker already shows, and never try to get around it.

    python tools/collect_spectrum.py
    python tools/collect_spectrum.py --backfill-pages 0 --max-bodies 100
    python tools/collect_spectrum.py --probe          # 1 Devtracker page + 1 post body, print, write nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rsi  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
POSTS = ROOT / "chronology" / "devtracker.json"
STATE = ROOT / "chronology" / "devtracker_state.json"
BODIES = ROOT / "devposts"
MAX_TRIES = 3


def load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


tracker_page = rsi.tracker_page          # shared with the Toolbox's live Dev Tracker tab

def _merge(posts: list[dict], found: list[dict], known: set) -> list[dict]:
    fresh = [p for p in found if p["id"] not in known]
    for p in fresh:
        known.add(p["id"])
    return fresh


def update_new(client: rsi.Client, posts: list[dict], state: dict, today: dt.date, max_pages: int = 60) -> int:
    known = {p["id"] for p in posts}
    added: list[dict] = []
    day = (today + dt.timedelta(days=1)).isoformat()    # a future day makes page 1 start with its own header
    last_full, ran_out = 0, False
    for page in range(1, max_pages + 1):
        found, day = tracker_page(client, page, day)
        if not found:
            ran_out = True
            break
        last_full = page
        fresh = _merge(posts, found, known)
        added.extend(fresh)
        if not fresh:
            break
    posts[:0] = added
    if "backfill_page" not in state:    # first run ever: the backfill continues from where this walk ended
        state["backfill_page"], state["backfill_day"] = last_full + 1, day
        if ran_out:
            state["backfill_done"] = True
    print(f"devtracker: {len(added)} new post(s)")
    for p in added[:40]:
        print(f"  + {p['date']}  {p['author']:<16} {p['category']:<14} {p['thread'][:60]}")
    return len(added)


def backfill(client: rsi.Client, posts: list[dict], state: dict, pages: int) -> int:
    if state.get("backfill_done") or pages <= 0:
        return 0
    known = {p["id"] for p in posts}
    page = int(state.get("backfill_page") or 1)
    day = state.get("backfill_day") or (dt.date.today() + dt.timedelta(days=1)).isoformat()
    added = 0
    for _ in range(pages):
        found, day = tracker_page(client, page, day)
        if not found:
            state["backfill_done"] = True
            break
        fresh = _merge(posts, found, known)
        posts.extend(fresh)             # older than everything we had: they go at the end (newest-first order)
        added += len(fresh)
        page += 1
        state["backfill_page"], state["backfill_day"] = page, day
    print(f"backfill: +{added} older post(s), next page {state.get('backfill_page')}"
          f"{' (done)' if state.get('backfill_done') else ''}")
    return added


def _write_body(p: dict, reply: dict, bodies: dict) -> bool:
    text = rsi.post_text(reply)
    ts = reply.get("time_created")
    if isinstance(ts, (int, float)) and ts > 0:
        p["time"] = int(ts)
        p["date"] = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()
    if len(text.split()) < 1:
        bodies[p["id"]] = {"s": "empty", "n": (bodies.get(p["id"]) or {}).get("n", 0) + 1}
        return False
    (BODIES / f"{p['id']}.txt").write_text(text + "\n", encoding="utf-8")
    bodies[p["id"]] = {"s": "ok", "w": len(text.split())}
    return True


def fetch_bodies(client: rsi.Client, posts: list[dict], state: dict, max_bodies: int) -> dict:
    bodies = state.setdefault("bodies", {})
    by_id = {p["id"]: p for p in posts}
    todo = [p for p in posts                                           # newest first
            if (bodies.get(p["id"]) or {}).get("s") not in ("ok", "private")
            and (bodies.get(p["id"]) or {}).get("n", 0) < MAX_TRIES]
    BODIES.mkdir(exist_ok=True)
    stats = {"ok": 0, "private": 0, "err": 0, "requests": 0}
    for p in todo:
        if stats["requests"] >= max_bodies:
            break
        b = bodies.get(p["id"]) or {}
        if b.get("s") in ("ok", "private"):
            continue                    # filled in already by an earlier thread in this run
        stats["requests"] += 1
        try:
            j = client.json("/api/spectrum/forum/thread/nested",
                            {"slug": p["slug"], "sort": "newest", "target_reply_id": p["reply_id"] or None})
        except Exception as exc:        # one bad thread must not stop the run; recorded and retried next time
            bodies[p["id"]] = {"s": "err", "n": b.get("n", 0) + 1, "e": f"{type(exc).__name__}: {exc}"[:200]}
            stats["err"] += 1
            continue
        if not j.get("success"):
            if j.get("code") == "ErrPermissionDenied":
                bodies[p["id"]] = {"s": "private"}
                stats["private"] += 1
            else:
                bodies[p["id"]] = {"s": "err", "n": b.get("n", 0) + 1, "e": str(j.get("code"))[:80]}
                stats["err"] += 1
            continue
        thread = j.get("data") or {}
        reply = rsi.find_reply(thread, p["reply_id"])
        if reply is None:
            bodies[p["id"]] = {"s": "err", "n": b.get("n", 0) + 1, "e": "reply not in thread payload"}
            stats["err"] += 1
        elif _write_body(p, reply, bodies):
            stats["ok"] += 1
        # The same payload often holds other tracked posts from this thread: keep those too, for free.
        for r in rsi.all_replies(thread):
            other = by_id.get(str(r.get("id")))
            if other is not None and other is not p and (bodies.get(other["id"]) or {}).get("s") != "ok" \
                    and other.get("slug") == p["slug"]:
                if _write_body(other, r, bodies):
                    stats["ok"] += 1
        if stats["requests"] % 50 == 0:
            save(POSTS, posts)
            save(STATE, state)          # checkpoint: a cancelled run keeps what it fetched
    left = sum(1 for p in posts if (bodies.get(p["id"]) or {}).get("s") not in ("ok", "private")
               and (bodies.get(p["id"]) or {}).get("n", 0) < MAX_TRIES)
    print(f"bodies: {stats['ok']} saved, {stats['private']} private (teaser only), {stats['err']} failed, "
          f"{left} still to do")
    return stats


def probe(client: rsi.Client) -> int:
    day = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    found, _ = tracker_page(client, 1, day)
    print(f"devtracker page 1: {len(found)} posts")
    for p in found[:6]:
        print(f"  {p['date']}  {p['author']:<16} {p['category']:<14} {p['thread'][:60]}")
    for p in found:
        j = client.json("/api/spectrum/forum/thread/nested",
                        {"slug": p["slug"], "sort": "newest", "target_reply_id": p["reply_id"] or None})
        if j.get("success"):
            reply = rsi.find_reply(j["data"], p["reply_id"])
            text = rsi.post_text(reply or {})
            print(f"\nbody of {p['author']} in {p['thread']!r}: {len(text.split())} words\n{text[:600]}")
            break
        print(f"  ({p['thread'][:40]}: {j.get('code')})")
    return 0 if found else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill-pages", type=int, default=150)
    ap.add_argument("--max-bodies", type=int, default=1200, help="thread requests per run")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args(argv)
    client = rsi.Client(delay=a.delay)
    if a.probe:
        return probe(client)
    posts = load(POSTS, [])
    state = load(STATE, {})
    try:
        update_new(client, posts, state, dt.date.today())
        backfill(client, posts, state, a.backfill_pages)
        if a.max_bodies > 0:
            fetch_bodies(client, posts, state, a.max_bodies)
    finally:
        save(POSTS, posts)
        save(STATE, state)
    print(f"{client.requests} request(s) to RSI")
    return 0


if __name__ == "__main__":
    sys.exit(main())
