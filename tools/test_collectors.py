"""test_collectors.py - run both collectors end to end against a fake RSI, in a throwaway copy of the data files.

    python tools/test_collectors.py

No network. Checks the incremental logic that matters for a daily job: new items found and dated, a known page
stops the walk, bodies fetched in priority order within budget, private Spectrum forums kept as teaser-only,
failures retried, and a second run changes nothing.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_commlinks as CC  # noqa: E402
import collect_spectrum as CS  # noqa: E402
import rsi  # noqa: E402

TODAY = dt.date.today()


def card(cid, ty, slug, title, age, teaser):
    return (f'<a class="content-block2 hub-block" href="/comm-link/{ty}/{cid}-{slug}"><div class="title-holder">'
            f'<div class="title trans-opacity">{title}</div></div><div class="text"><div class="time_ago">'
            f'<span class="value">{age}</span></div></div><div class="over"><div class="body"><p>{teaser}</p>'
            f'</div></div></a>')


LISTING = {
    1: card(900, "transmission", "This-Week", "This Week in Star Citizen", "2 days ago", "Stay up to date")
       + card(899, "transmission", "Star-Citizen-Monthly-Report-September-2026", "Star Citizen Monthly Report: "
              "September 2026", "3 days ago", "Welcome to September")
       + card(500, "spectrum-dispatch", "Old-Lore", "Old Lore", "2016-01-01 10:00:00", "lore"),
    2: card(499, "transmission", "Older", "Older", "2015-01-01 10:00:00", "older"),
}
ARTICLES = {
    "/comm-link/transmission/899-Star-Citizen-Monthly-Report-September-2026":
        "<script>const s3Url = 'https://robertsspaceindustries.com/alexandria/html/a/b/mr-en'; fetch(s3Url)</script>",
    "/alexandria/html/a/b/mr-en":
        '<g-introduction :info="{&quot;title&quot;:&quot;PU Monthly Report&quot;,&quot;contents&quot;:&quot;&lt;p&gt;'
        'Welcome to the September report from all the teams.&lt;/p&gt;&quot;}"></g-introduction><g-article body="'
        '&lt;h2&gt;Vehicles&lt;/h2&gt;&lt;p&gt;The Hull C got a new cargo grid this month.&lt;/p&gt;"></g-article>',
    "/comm-link/transmission/900-This-Week":
        '<div id="post"><div class=" segment"><p>Monday brings a new episode of Inside Star Citizen.</p></div></div>',
    "/comm-link/spectrum-dispatch/500-Old-Lore": "BROKEN",
}
TRACKER = {
    1: '<h3>September 26th 2026</h3>'
       '<a class="devpost" href="/spectrum/community/SC/forum/3/thread/life-support/111"><div class="nickname">Nicou-CIG'
       '</div><span class="category">General</span><span class="thread">Life support?</span><p class="details">'
       'We have been tracking this...</p></a>'
       '<a class="devpost" href="/spectrum/community/SC/forum/190049/thread/netcode-preview/222"><div class="nickname">'
       'mkale-CIG</div><span class="category">Focus Testing</span><span class="thread">Netcode preview</span>'
       '<p class="details">stay tuned</p></a>',
    2: '<h3>September 25th 2026</h3><a class="devpost" href="/spectrum/community/SC/forum/1/thread/patch-notes">'
       '<div class="nickname">Shark-CIG</div><span class="category">Patch</span><span class="thread">Patch Notes 4.10.2'
       '</span><p class="details">Greetings</p></a>',
}
THREADS = {
    "life-support": {"success": 1, "data": {"id": "5", "replies": [{"id": "111", "time_created": 1790441386,
        "content_blocks": [{"type": "text", "data": {"blocks": [{"type": "unstyled",
            "text": "Hey folks, we have been tracking the life support bug and a fix is in 4.10.2."}]}}]}]}},
    "netcode-preview": {"success": 0, "code": "ErrPermissionDenied"},
    "patch-notes": {"success": 1, "data": {"id": "6", "time_created": 1790300000, "content_blocks": [
        {"type": "text", "data": {"blocks": [{"type": "header-one", "text": "Alpha 4.10.2"},
                                             {"type": "unordered-list-item", "text": "Fixed life support"}]}}]}},
}


class Fake:
    def __init__(self):
        self.log = []

    def __call__(self, req, timeout=0):
        path = req.full_url.replace(rsi.BASE, "")
        body = json.loads(req.data) if req.data else None
        self.log.append(path)
        if path == "/api/hub/getCommlinkItems":
            out = {"success": 1, "data": LISTING.get(body["page"], "")}
        elif path == "/api/community/getTrackedPosts":
            out = {"success": 1, "data": {"html": TRACKER.get(body["page"], ""), "date": "", "count": 0}}
        elif path == "/api/spectrum/forum/thread/nested":
            out = THREADS[body["slug"]]
        elif path in ARTICLES:
            if ARTICLES[path] == "BROKEN":
                raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
            return io.BytesIO(ARTICLES[path].encode())
        else:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        return io.BytesIO(json.dumps(out).encode())


def main() -> int:
    ok = True

    def case(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "chronology").mkdir()
        # Already archived: 500 and 499 (so page 1 has two new items, page 2 is fully known).
        (root / "chronology" / "commlink_index.json").write_text(json.dumps([
            {"id": "500", "type": "spectrum-dispatch", "url": rsi.BASE + "/comm-link/spectrum-dispatch/500-Old-Lore",
             "title": "Old Lore", "age_text": "2016-01-01 10:00:00", "teaser": "lore", "_page": 5},
            {"id": "499", "type": "transmission", "url": rsi.BASE + "/comm-link/transmission/499-Older",
             "title": "Older", "age_text": "2015-01-01", "teaser": "older", "_page": 6}]))
        (root / "chronology" / "web_records.json").write_text(json.dumps([
            {"source": "rsi-comm-link", "type": "transmission", "title": "Older", "date": "2015-01-01",
             "url": rsi.BASE + "/comm-link/transmission/499-Older", "summary": "older"},
            {"source": "rsi-comm-link", "type": "spectrum-dispatch", "title": "Old Lore", "date": "2016-01-01",
             "url": rsi.BASE + "/comm-link/spectrum-dispatch/500-Old-Lore", "summary": "lore"}]))
        CC.INDEX, CC.RECORDS = root / "chronology" / "commlink_index.json", root / "chronology" / "web_records.json"
        CC.STATE, CC.BODIES = root / "chronology" / "commlink_bodies.json", root / "commlinks"
        CS.POSTS, CS.STATE, CS.BODIES = (root / "chronology" / "devtracker.json",
                                         root / "chronology" / "devtracker_state.json", root / "devposts")

        fake = Fake()
        client = rsi.Client(delay=0, retries=0, opener=fake)
        n = CC.update_listing(client, TODAY)
        recs = json.loads(CC.RECORDS.read_text())
        case("comm-links: the two new ones added", n == 2 and len(recs) == 4)
        case("comm-links: walk stops on the first fully known page", fake.log.count("/api/hub/getCommlinkItems") == 2)
        mr = next(r for r in recs if "Monthly" in r["title"])
        case("comm-links: new item dated from '3 days ago'",
             mr["date"] == (TODAY - dt.timedelta(days=3)).isoformat() and mr["date_precision"] == "day")
        case("comm-links: records stay sorted by date", [r["date"] for r in recs] == sorted(r["date"] for r in recs))
        case("comm-links: raw index newest-first", json.loads(CC.INDEX.read_text())[0]["id"] == "900")

        fake.log.clear()
        CC.fetch_bodies(client, max_bodies=2)
        state = json.loads(CC.STATE.read_text())
        case("bodies: Monthly Report fetched first", fake.log[0].endswith("899-Star-Citizen-Monthly-Report-"
                                                                            "September-2026"))
        case("bodies: budget respected (2 articles)", len(state) == 2)
        mr_text = (CC.BODIES / "899.txt").read_text()
        case("bodies: alexandria body with heading", "## Vehicles\nThe Hull C got a new cargo grid" in mr_text)
        CC.fetch_bodies(client, max_bodies=10)
        CC.fetch_bodies(client, max_bodies=10)
        CC.fetch_bodies(client, max_bodies=10)
        state = json.loads(CC.STATE.read_text())
        case("bodies: legacy #post body saved", (CC.BODIES / "900.txt").read_text().startswith("Monday brings"))
        case("bodies: a failing article is retried, then left alone", state["500"]["s"] == "err"
             and state["500"]["n"] == CC.MAX_TRIES)
        fake.log.clear()
        CC.fetch_bodies(client, max_bodies=10)
        case("bodies: nothing refetched once done", not any("comm-link/" in p for p in fake.log))

        fake.log.clear()
        posts, st = [], {}
        CS.update_new(client, posts, st, TODAY)
        case("devtracker: all posts across pages", [p["id"] for p in posts] == ["111", "222", "t-1-patch-notes"])
        case("devtracker: day headers", [p["date"] for p in posts] == ["2026-09-26", "2026-09-26", "2026-09-25"])
        case("devtracker: first run seeds the backfill position (and sees the tracker ran out)",
             st["backfill_page"] == 3 and st.get("backfill_done") is True)
        CS.fetch_bodies(client, posts, st, max_bodies=10)
        b = st["bodies"]
        case("devtracker: public reply body saved",
             (CS.BODIES / "111.txt").read_text().startswith("Hey folks, we have been tracking"))
        case("devtracker: private forum kept as teaser only", b["222"] == {"s": "private"}
             and not (CS.BODIES / "222.txt").exists())
        case("devtracker: opening post body", (CS.BODIES / "t-1-patch-notes.txt").read_text()
             == "## Alpha 4.10.2\n- Fixed life support\n")
        case("devtracker: exact date from the post timestamp", posts[0]["time"] == 1790441386)
        n_before = len(fake.log)
        CS.update_new(client, posts, st, TODAY)
        CS.fetch_bodies(client, posts, st, max_bodies=10)
        case("devtracker: second run adds nothing and fetches no bodies", len(posts) == 3
             and not any("thread/nested" in p for p in fake.log[n_before:]))
        st2 = {"backfill_page": 2, "backfill_day": "2026-09-26"}
        found_before = len(posts)
        CS.backfill(client, posts, st2, pages=5)
        case("devtracker: backfill resumes, dedupes, and ends when the tracker runs out",
             st2.get("backfill_done") is True and len(posts) == found_before and st2["backfill_page"] == 3)
    print("collectors test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
