"""build_index.py - build the small search index the SC Toolbox streams (chronology/search_index.json.gz).

The corpus is tens of MB; nobody should download it to search it. This builds one compact inverted index over every
transcript, comm-link and Devtracker post: per document only its metadata, and per term the documents it appears in
with a count. A client downloads this once (a few MB, gzipped), searches it offline, and fetches a single text file
from raw.githubusercontent.com only when a result is opened (to show the matching lines, or the whole article).

Document kinds ("k"):  v = dev video transcript   c = RSI comm-link   d = CIG post from the Spectrum Devtracker
A doc with a "p" has a full text file at that path in this repo. "g": "mr" marks a Monthly Report. "s" is a summary:
for comm-links and dev posts with full text it is a digest of what they say ("sd": 1; a short dev post is its own
digest), otherwise the teaser RSI shows.

    python tools/build_index.py          # from the repo root
"""
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rsi  # noqa: E402  (summaries of long dev posts)

SHORT_POST = 60        # words; a dev post this short is shown whole instead of summarized
MONTHLY = re.compile(r"\bmonthly (studio )?report\b", re.I)
WORD = re.compile(r"[a-z0-9][a-z0-9'\-]{2,}")
STOP = set("""the and for that this with you are was but not have they what there from just going can all about
we're it's that's i'm like know yeah really one would get think out some more also which will when were been them
their then than into our your its his her she him has had how who why where well very okay right thing things
lot kind sort gonna want make made let see look need because those these other over only even much any here now
""".split())


def tokens(text):
    return [w for w in WORD.findall(text.lower()) if w not in STOP]


def main():
    meta = json.loads((ROOT / "chronology" / "chronology_meta.json").read_text(encoding="utf-8"))
    whisper = {}
    wi = ROOT / "transcripts" / "whisper" / "_index.jsonl"
    for line in wi.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            whisper[r["id"]] = r
    # Titles recovered for videos missing from chronology_meta (oEmbed; dates there are estimates).
    patch = {r["id"]: r for r in json.loads((ROOT / "chronology" / "title_patch.json").read_text(encoding="utf-8"))}
    docs, postings = [], defaultdict(Counter)

    def add(doc, text):
        i = len(docs)
        docs.append(doc)
        for w, c in Counter(tokens(text)).items():
            postings[w][i] = c

    for kind in ("captions", "whisper"):
        for f in sorted((ROOT / "transcripts" / kind).glob("*.txt")):
            vid = f.stem
            m = meta.get(vid) or patch.get(vid) or {}
            w = whisper.get(vid) or {}
            date = str(m.get("date") or w.get("upload_date") or "")
            if len(date) == 8 and date.isdigit():
                date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
            add({"id": vid, "k": "v", "t": m.get("title") or w.get("title") or vid, "d": date,
                 "p": f"transcripts/{kind}/{vid}.txt"},
                f.read_text(encoding="utf-8", errors="replace"))
    n_bodies = 0
    for r in json.loads((ROOT / "chronology" / "web_records.json").read_text(encoding="utf-8")):
        doc = {"id": r["url"], "k": "c", "t": r["title"], "d": r.get("date", ""),
               "s": r.get("digest") or r.get("summary", ""), "u": r["url"], "ty": r.get("type", "")}
        if r.get("digest"):
            doc["sd"] = 1                # "s" is a digest of the article, not RSI's teaser
        if MONTHLY.search(r["title"]):
            doc["g"] = "mr"
        body = ""
        m = re.search(r"/comm-link/[\w-]+/(\d+)-", r["url"])
        f = ROOT / "commlinks" / f"{m.group(1)}.txt" if m else None
        if f is not None and f.exists():
            body = f.read_text(encoding="utf-8", errors="replace")
            doc["p"] = f"commlinks/{f.name}"
            n_bodies += 1
        add(doc, f"{r['title']} {r.get('summary', '')} {body}")
    n_dev = 0
    dev_path = ROOT / "chronology" / "devtracker.json"
    for r in json.loads(dev_path.read_text(encoding="utf-8")) if dev_path.exists() else []:
        doc = {"id": r["id"], "k": "d", "t": r.get("thread") or "(Spectrum post)", "d": r.get("date", ""),
               "s": r.get("teaser", ""), "u": r["url"], "a": r.get("author", ""), "c": r.get("category", "")}
        body = ""
        f = ROOT / "devposts" / f"{r['id']}.txt"
        if f.exists():
            body = f.read_text(encoding="utf-8", errors="replace")
            doc["p"] = f"devposts/{f.name}"
            n_dev += 1
            words = len(body.split())
            if words <= SHORT_POST:
                doc["s"] = body.strip()          # short: the whole post is its own summary
            else:
                doc["s"] = rsi.summarize(body, doc["t"])
            doc["sd"] = 1
        else:                                     # not transcribed yet: enough for a client to fetch it itself
            doc["slug"], doc["reply_id"] = r.get("slug", ""), r.get("reply_id", "")
        add(doc, f"{doc['t']} {r.get('author', '')} {r.get('category', '')} {r.get('teaser', '')} {body}")
    # Terms in nearly every document carry no signal and dominate the size.
    cap = len(docs) * 0.6
    idx = {w: {str(i): c for i, c in p.items()} for w, p in postings.items() if len(p) < cap}
    out = {"version": 2, "n_docs": len(docs), "docs": docs, "postings": idx}
    raw = json.dumps(out, separators=(",", ":")).encode("utf-8")
    dest = ROOT / "chronology" / "search_index.json.gz"
    dest.write_bytes(gzip.compress(raw, 9))
    kinds = Counter(d["k"] for d in docs)
    print(f"videos {kinds['v']}, comm-links {kinds['c']} ({n_bodies} with full text), "
          f"dev posts {kinds['d']} ({n_dev} with full text)")
    print(f"{len(docs)} docs, {len(idx)} terms, {len(raw)/1e6:.1f} MB raw, {dest.stat().st_size/1e6:.1f} MB gzipped")


if __name__ == "__main__":
    main()
