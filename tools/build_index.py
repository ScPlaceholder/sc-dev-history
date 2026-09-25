"""build_index.py - build the small search index the SC Toolbox streams (chronology/search_index.json.gz).

The corpus is ~51 MB; nobody should download it to search it. This builds one compact inverted index over every
transcript and comm-link record: per document only its metadata, and per term the documents it appears in with a
count. A client downloads this once (a few MB, gzipped), searches it offline, and fetches a single transcript from
raw.githubusercontent.com only when a result is opened (to show the matching lines).

    python tools/build_index.py          # from the repo root
"""
import gzip
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
    for r in json.loads((ROOT / "chronology" / "web_records.json").read_text(encoding="utf-8")):
        add({"id": r["url"], "k": "c", "t": r["title"], "d": r.get("date", ""), "s": r.get("summary", ""),
             "u": r["url"], "ty": r.get("type", "")},
            f"{r['title']} {r.get('summary', '')}")
    # Terms in nearly every document carry no signal and dominate the size.
    cap = len(docs) * 0.6
    idx = {w: {str(i): c for i, c in p.items()} for w, p in postings.items() if len(p) < cap}
    out = {"version": 1, "n_docs": len(docs), "docs": docs, "postings": idx}
    raw = json.dumps(out, separators=(",", ":")).encode("utf-8")
    dest = ROOT / "chronology" / "search_index.json.gz"
    dest.write_bytes(gzip.compress(raw, 9))
    print(f"{len(docs)} docs, {len(idx)} terms, {len(raw)/1e6:.1f} MB raw, {dest.stat().st_size/1e6:.1f} MB gzipped")


if __name__ == "__main__":
    main()
