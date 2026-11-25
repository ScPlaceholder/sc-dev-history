"""import_local_cache.py - bring what an SC Toolbox install fetched live from RSI into this repo.

The Dev History tool keeps what it fetches from robertsspaceindustries.com (Dev Tracker posts, their full text,
comm-link articles such as Monthly Reports) under ~/.sctoolbox/dev_history/live/. This merges it into the archive
so every user gets it, and it is the fallback when RSI blocks GitHub's runners: fetch at home, import, push.

    python tools/import_local_cache.py                      # from ~/.sctoolbox/dev_history/live
    python tools/import_local_cache.py --from D:/somewhere/live
    python tools/build_index.py                             # then rebuild the index, commit, push

What it does, all idempotent (run it again after more fetching; nothing is duplicated):
  * live/devtracker.json   -> chronology/devtracker.json   new posts added; existing ones keep the repo's data and
                                                           only gain fields they were missing
  * live/devposts/<id>.txt -> devposts/<id>.txt             marked fetched in chronology/devtracker_state.json
  * live/commlinks/<n>.txt -> commlinks/<n>.txt             marked fetched in chronology/commlink_bodies.json,
                                                           with a digest in chronology/web_records.json
  * live/devtracker_state.json "done" -> the archiver's Devtracker backfill is marked complete too
The repo's own files always win: a text already in the repo is never overwritten.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rsi  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LIVE = Path.home() / ".sctoolbox" / "dev_history" / "live"
POST_FIELDS = ("reply_id", "community", "forum_id", "slug", "url", "author", "category", "thread", "teaser", "date",
               "time")


def load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def import_posts(live: Path, root: Path) -> dict:
    posts_path = root / "chronology" / "devtracker.json"
    state_path = root / "chronology" / "devtracker_state.json"
    posts = load(posts_path, [])
    state = load(state_path, {})
    bodies = state.setdefault("bodies", {})
    by_id = {p["id"]: p for p in posts}
    added = filled = 0
    for lp in load(live / "devtracker.json", []):
        pid = lp.get("id")
        if not pid or not lp.get("slug"):
            continue
        mine = by_id.get(pid)
        if mine is None:
            p = {"id": pid, **{k: lp[k] for k in POST_FIELDS if lp.get(k) not in (None, "")}}
            p.setdefault("reply_id", "")
            posts.append(p)
            by_id[pid] = p
            added += 1
        else:
            for k in POST_FIELDS:
                if mine.get(k) in (None, "") and lp.get(k) not in (None, ""):
                    mine[k] = lp[k]
                    filled += 1
    posts.sort(key=lambda p: (p.get("date") or "", p.get("time") or 0, p["id"]), reverse=True)

    texts = 0
    (root / "devposts").mkdir(exist_ok=True)
    for f in sorted((live / "devposts").glob("*.txt")) if (live / "devposts").is_dir() else []:
        pid = f.stem
        dest = root / "devposts" / f.name
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        if pid not in by_id or not text or dest.exists():
            continue
        dest.write_text(text + "\n", encoding="utf-8")
        bodies[pid] = {"s": "ok", "w": len(text.split()), "src": "toolbox"}
        texts += 1

    lstate = load(live / "devtracker_state.json", {})
    if lstate.get("done"):
        state["backfill_done"] = True           # the whole Devtracker list is in; no need to walk it again
    save(posts_path, posts)
    save(state_path, state)
    return {"posts_added": added, "fields_filled": filled, "post_texts": texts, "posts_total": len(posts),
            "history_complete": bool(state.get("backfill_done"))}


def import_commlinks(live: Path, root: Path) -> dict:
    rec_path = root / "chronology" / "web_records.json"
    state_path = root / "chronology" / "commlink_bodies.json"
    records = load(rec_path, [])
    state = load(state_path, {})
    by_num = {}
    for r in records:
        m = re.search(r"/comm-link/[\w-]+/(\d+)-", r.get("url", ""))
        if m:
            by_num[m.group(1)] = r
    texts = skipped = 0
    (root / "commlinks").mkdir(exist_ok=True)
    for f in sorted((live / "commlinks").glob("*.txt")) if (live / "commlinks").is_dir() else []:
        r = by_num.get(f.stem)
        dest = root / "commlinks" / f.name
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        if r is None or len(text.split()) < 5 or dest.exists():
            skipped += 1
            continue
        dest.write_text(text + "\n", encoding="utf-8")
        r["digest"] = rsi.summarize(text, r.get("title", ""))
        state[f.stem] = {"s": "ok", "w": len(text.split()), "src": "toolbox"}
        texts += 1
    save(rec_path, records)
    save(state_path, state)
    return {"commlink_texts": texts, "commlinks_skipped": skipped}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="live", type=Path, default=DEFAULT_LIVE,
                    help=f"the Toolbox's live folder (default {DEFAULT_LIVE})")
    ap.add_argument("--root", type=Path, default=ROOT, help="repo root (default: this repo)")
    a = ap.parse_args(argv)
    if not a.live.is_dir():
        print(f"no such folder: {a.live}")
        return 2
    res = import_posts(a.live, a.root)
    res.update(import_commlinks(a.live, a.root))
    for k, v in res.items():
        print(f"  {k}: {v}")
    print("next: python tools/build_index.py, then commit and push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
