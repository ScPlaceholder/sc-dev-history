"""collect_videos.py - find CIG's new videos and finished streams, transcribe them locally, add them to the corpus.

Runs on a PC, not in the GitHub Action: it needs a home connection (YouTube blocks datacenter IPs) and a local
Whisper. One run does this, one video at a time:

  1. LIST. Ask YouTube for the newest entries of each source in tools/collect_videos.json (one light request per
     source). A video is NEW when the corpus does not have it and it was published on or after "not_before".
     Unknown videos older than that are "older gaps": listed, never taken unless you pass --backfill.
  2. For each new video, oldest first: download the AUDIO TRACK ONLY, transcribe it with faster-whisper on the CPU
     at below-normal priority, write transcripts/whisper/<id>.txt in the existing format, add it to the three
     indexes the repo keeps for Whisper transcripts (transcripts/whisper/_index.jsonl, chronology/records.json,
     chronology/playlist_ids_oldest_last.json), then delete the audio.
  3. Remember what happened in chronology/video_state.json: ids done, and ids that failed with the reason (retried
     on later runs, up to "max_tries", then left alone). A crash costs one video.
  4. COMMIT exactly the files it wrote (never `git add -A`), as the configured author with one Co-Authored-By
     trailer. No commit when nothing changed. It pushes only when "push" is true in the config.

It is built to be left alone: a pause between downloads, a cap on videos and on audio hours per run, anything
still live is skipped, and if YouTube answers with its "confirm you're not a bot" check (or HTTP 429) the run
stops at once, the check is recorded, and nothing contacts YouTube again until the cooldown has passed. A video
whose media download fails (HTTP 403 and the like) is recorded as failed and the run moves on. A second copy
refuses to start while one is running.

The search index (chronology/search_index.json.gz) is NOT rebuilt here by default: the daily GitHub Action rebuilds
it from whatever is on the branch, so a pushed transcript is searchable within a day and the two never fight over
the same 9 MB file. Set "rebuild_search_index": true to rebuild and commit it locally instead.

    python tools/collect_videos.py --dry-run        # list what a run WOULD do; touches nothing
    python tools/collect_videos.py                  # a real run within the config's caps
    python tools/collect_videos.py --limit 2        # at most 2 videos this run
    python tools/collect_videos.py --hours 10       # raise the audio-hours cap for this run (a long broadcast)
    python tools/collect_videos.py --backfill       # also take the older gaps (still within the caps)
    python tools/collect_videos.py --selftest       # every rule above against a fake YouTube (no network)

Exit codes: 0 fine (also "nothing new") . 1 a video failed . 2 refused (wrong branch, bad config, low disk)
            3 stopped by a bot check or still cooling down . 4 another copy is running . 5 the commit is not what
            it should be (nothing is pushed in that case).

Local-only files live in _video_work/ (ignores itself in git): the lock, the log, the audio while it is being
transcribed, the bot-check cooldown and the list of files written but not committed yet.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG = HERE / "collect_videos.json"
WATCH = "https://www.youtube.com/watch?v=%s"
LIVE = ("is_live", "is_upcoming", "post_live")      # post_live: just ended, YouTube is still processing it
SEG = re.compile(r"^\[\s*([\d.]+)\] (.*)$")

DEFAULTS = {
    "sources": [
        {"name": "youtube-videos", "url": "https://www.youtube.com/@RobertsSpaceInd/videos", "enabled": True},
        {"name": "youtube-streams", "url": "https://www.youtube.com/@RobertsSpaceInd/streams", "enabled": True},
    ],
    "not_before": "2026-06-19",
    "scan_newest": 30,
    "max_items_per_run": 4,
    "max_audio_hours_per_run": 4.0,
    "pause_seconds": 45,
    "max_tries": 3,
    "botcheck_cooldown_hours": 12,
    "min_free_disk_gb": 3,
    "download_timeout_seconds": 3600,
    "whisper_model": "small",
    "device": "cpu",
    "compute_type": "int8",
    "cpu_threads": 4,
    "beam_size": 1,
    "language": "en",
    "below_normal_priority": True,
    "branch": "corpus",
    "remote": "origin",
    "push": False,
    "rebuild_search_index": False,
    "author_name": "ElahMoth",
    "author_email": "322847132+elahmoth@users.noreply.github.com",
    "co_author": "ScPlaceholder <starcitizenplaceholder@gmail.com>",
}

BOT = re.compile(r"confirm you.{0,3}re not a bot|HTTP Error 429|Too Many Requests", re.I)


class BotCheck(Exception):
    """YouTube asked for proof of humanity (or rate-limited us). The run must stop."""


class Refused(Exception):
    """The run may not start or continue (wrong branch, low disk...). Nothing is wrong with a video."""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify(stderr: str) -> tuple[str, str]:
    """What a failed yt-dlp call means: ("botcheck" | "failed", short reason)."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.startswith("ERROR")] or lines
    last = (errors[-1] if errors else "no output from yt-dlp")[:200]
    if BOT.search(stderr or ""):
        return "botcheck", last
    if "HTTP Error 403" in (stderr or ""):
        return "failed", "http_403: " + last
    if "TIMEOUT" in (stderr or ""):
        return "failed", "timeout: " + last
    return "failed", last


def run_child(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    """Run a Python child and read its output as UTF-8. (rc, stdout, stderr); rc 124 on timeout.
    The child is TOLD to write UTF-8: on Windows a piped Python writes cp1252 otherwise, and a title with a curly
    apostrophe arrives with the apostrophe destroyed (it did, on the first real run of this tool)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=flags, env=env)
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"ERROR: TIMEOUT after {timeout}s"


def run_ytdlp(args: list[str], timeout: int) -> tuple[int, str, str]:
    """The only place that talks to YouTube."""
    return run_child([sys.executable, "-m", "yt_dlp"] + args, timeout)


def acquire_lock(path: Path):
    """An OS lock on a file: held while the handle is open, released by the OS if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def lower_priority() -> None:
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)      # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except Exception:
        pass


def load_config(path: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if path.exists():
        for k, v in json.loads(path.read_text(encoding="utf-8")).items():
            if not k.startswith("_"):
                cfg[k] = v
    return cfg


def clean_title(t: str) -> str:
    return " ".join((t or "").split())


def fmt_dur(seconds) -> str:
    if not seconds:
        return "   ?   "
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


class Collector:
    def __init__(self, root: Path, cfg: dict, dry: bool = False):
        self.root, self.cfg, self.dry = Path(root), cfg, dry
        self.whisper = self.root / "transcripts" / "whisper"
        self.captions = self.root / "transcripts" / "captions"
        self.index = self.whisper / "_index.jsonl"
        self.records = self.root / "chronology" / "records.json"
        self.playlist = self.root / "chronology" / "playlist_ids_oldest_last.json"
        self.state_path = self.root / "chronology" / "video_state.json"
        self.search_index = self.root / "chronology" / "search_index.json.gz"
        self.work = self.root / "_video_work"
        self.local_path = self.work / "local_state.json"
        self.journal_path = self.work / "uncommitted.json"
        self.log_path = self.work / "collect_videos.log"
        # seams the selftest replaces
        self.ytdlp = run_ytdlp
        self.transcribe = self._whisper
        self.sleep = time.sleep
        self.git_calls: list[list[str]] = []
        self._model = None
        self.state = self._load(self.state_path, {"version": 1, "done": {}, "failed": {}})
        self.state.setdefault("done", {})
        self.state.setdefault("failed", {})
        self.local = self._load(self.local_path, {})
        self.summary = {"done": [], "failed": [], "live": [], "nospeech": [], "audio_seconds": 0.0}

    # ---- small helpers ----------------------------------------------------------------------------------------------
    @staticmethod
    def _load(path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    @staticmethod
    def _save(path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(path)

    def say(self, msg: str) -> None:
        print(msg, flush=True)
        if self.dry:
            return
        try:
            self.work.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(f"{now_iso()} {msg}\n")
        except OSError:
            pass

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def journal(self) -> list[str]:
        return self._load(self.journal_path, [])

    def journal_add(self, path: Path) -> None:
        """Note a repo file as written-by-us BEFORE writing it, so a crash cannot orphan it from the next commit."""
        j = self.journal()
        r = self.rel(path)
        if r not in j:
            j.append(r)
            self._save(self.journal_path, j)

    def save_state(self) -> None:
        self.journal_add(self.state_path)
        self._save(self.state_path, self.state)

    def save_local(self) -> None:
        self._save(self.local_path, self.local)

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        argv = ["git", "-c", "core.quotepath=false", "-c", "core.safecrlf=false", *args]
        self.git_calls.append(list(args))
        r = subprocess.run(argv, cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[:300]}")
        return r

    # ---- what we already have ---------------------------------------------------------------------------------------
    def has_transcript(self, vid: str) -> bool:
        if (self.captions / f"{vid}.txt").exists():
            return True
        f = self.whisper / f"{vid}.txt"
        try:
            if f.stat().st_size > 0:                # a zero-byte file is a crashed write, not a transcript
                with open(f, encoding="utf-8", errors="replace") as fh:
                    return fh.readline().startswith("# ")
        except OSError:
            pass
        return False

    def is_known(self, vid: str) -> bool:
        return vid in self.state["done"] or self.has_transcript(vid)

    # ---- listing ----------------------------------------------------------------------------------------------------
    def list_source(self, src: dict) -> list[dict]:
        args = ["--flat-playlist", "--playlist-end", str(int(self.cfg["scan_newest"])), "--no-warnings",
                "--extractor-args", "youtubetab:approximate_date", "-J", src["url"]]
        rc, out, err = self.ytdlp(args, 300)
        if rc != 0 or not out.strip():
            kind, reason = classify(err)
            if kind == "botcheck":
                raise BotCheck(f"listing {src['name']}: {reason}")
            raise RuntimeError(f"listing {src['name']}: {reason}")
        entries = []
        for pos, e in enumerate(json.loads(out).get("entries") or []):
            if e and e.get("id"):
                entries.append({"id": e["id"], "title": clean_title(e.get("title") or e["id"]),
                                "duration": e.get("duration"), "live_status": e.get("live_status"),
                                "ts": e.get("timestamp") or e.get("release_timestamp"), "pos": pos,
                                "source": src["name"]})
        return entries

    def plan(self, by_source: list[list[dict]], limit, hours, backfill: bool) -> dict:
        """Sort every listed entry into: take (this run), deferred (caps), live, gaps (older, unknown), gave_up."""
        not_before = dt.date.fromisoformat(self.cfg["not_before"])
        max_items = int(self.cfg["max_items_per_run"]) if limit is None else int(limit)
        cap = float(self.cfg["max_audio_hours_per_run"]) if hours is None else float(hours)
        cands, live, gaps, gave_up, seen = [], [], [], [], set()
        for entries in by_source:
            first_known = next((e["pos"] for e in entries if self.is_known(e["id"])), None)
            for e in entries:
                vid = e["id"]
                if vid in seen or self.is_known(vid):
                    continue
                seen.add(vid)
                if e["live_status"] in LIVE:
                    live.append(e)
                    continue
                if self.state["failed"].get(vid, {}).get("n", 0) >= int(self.cfg["max_tries"]):
                    gave_up.append(e)
                    continue
                if e["ts"]:
                    is_new = dt.datetime.fromtimestamp(e["ts"], dt.timezone.utc).date() >= not_before
                else:                   # no date from YouTube: new only if it sits above everything we already have
                    is_new = first_known is None or e["pos"] < first_known
                (cands if (is_new or backfill) else gaps).append(e)
        cands.sort(key=lambda e: -e["pos"])                       # oldest first within a source...
        cands.sort(key=lambda e: e["ts"] or 9e18)                 # ...and across sources (stable)
        take, deferred, total = [], [], 0.0
        for e in cands:
            h = (e["duration"] or 0) / 3600.0
            if len(take) >= max_items:
                deferred.append((e, "over the videos-per-run cap"))
            elif h > cap:
                deferred.append((e, f"longer than the {cap:g} h per-run cap: run it by hand with --hours"))
            elif total + h > cap:
                deferred.append((e, "over the audio-hours-per-run cap"))
            else:
                take.append(e)
                total += h
        return {"take": take, "deferred": deferred, "live": live, "gaps": gaps, "gave_up": gave_up,
                "hours": total, "cap": cap, "max_items": max_items}

    # ---- one video --------------------------------------------------------------------------------------------------
    def download(self, vid: str) -> dict:
        """Audio track only. {"kind": "ok", "path", "meta"} | {"kind": "live"} | {"kind": "failed", "reason"}."""
        self.work.mkdir(parents=True, exist_ok=True)
        tmpl = str(self.work / f"audio_{vid}.%(ext)s")
        args = ["-f", "bestaudio/best", "--no-playlist", "--no-progress", "--no-warnings", "--no-simulate",
                "--socket-timeout", "30", "--retries", "3",
                "--match-filter", "!is_live & live_status!=is_upcoming & live_status!=post_live",
                "--print", "pre_process:META\t%(id)s\t%(upload_date)s\t%(duration)s\t%(live_status)s\t%(title)s",
                "-o", tmpl, WATCH % vid]
        rc, out, err = self.ytdlp(args, int(self.cfg["download_timeout_seconds"]))
        meta = {}
        for line in out.splitlines():
            p = line.split("\t", 5)
            if len(p) == 6 and p[0] == "META":
                meta = {"upload_date": p[2], "duration": p[3], "live_status": p[4], "title": clean_title(p[5])}
        files = [f for f in self.work.glob(f"audio_{vid}.*") if f.suffix not in (".part", ".ytdl")]
        if BOT.search(err):
            self.clean_audio(vid)
            raise BotCheck(f"download {vid}: {classify(err)[1]}")
        if meta.get("live_status") in LIVE or (rc == 0 and not files and "does not pass filter" in out + err):
            self.clean_audio(vid)
            return {"kind": "live"}
        if rc == 0 and files:
            return {"kind": "ok", "path": files[0], "meta": meta}
        self.clean_audio(vid)
        return {"kind": "failed", "reason": classify(err or "ERROR: yt-dlp wrote no audio file")[1]}

    def clean_audio(self, vid: str = "*") -> None:
        if self.work.is_dir():
            for f in self.work.glob(f"audio_{vid}.*"):
                try:
                    f.unlink()
                except OSError:
                    pass

    def _whisper(self, path: Path) -> tuple[list[tuple[float, str]], float]:
        if self._model is None:
            if self.cfg["device"] == "cpu":
                os.environ["CUDA_VISIBLE_DEVICES"] = "-1"       # belt and braces: the GPU is J's, never ours
            from faster_whisper import WhisperModel
            kw = dict(device=self.cfg["device"], compute_type=self.cfg["compute_type"],
                      cpu_threads=int(self.cfg["cpu_threads"]))
            try:                                                # a model already on disk: no network at all
                self._model = WhisperModel(self.cfg["whisper_model"], local_files_only=True, **kw)
            except Exception:
                self.say(f"  whisper model '{self.cfg['whisper_model']}' is not on disk yet: downloading it once")
                self._model = WhisperModel(self.cfg["whisper_model"], **kw)
        segs, info = self._model.transcribe(str(path), beam_size=int(self.cfg["beam_size"]), vad_filter=True,
                                            language=self.cfg.get("language") or None)
        out = [(float(s.start), s.text.strip()) for s in segs]
        return [(a, t) for a, t in out if t], float(info.duration or 0)

    def newline(self) -> str:
        """The working tree's line ending (CRLF on a Windows checkout with autocrlf), taken from the index file."""
        try:
            with open(self.index, "rb") as fh:
                return "\r\n" if b"\r\n" in fh.read(4096) else "\n"
        except OSError:
            return "\n"

    def write_transcript(self, vid: str, title: str, segs) -> Path:
        nl = self.newline()
        lines = [f"# {title}", "# " + WATCH % vid] + ["[%7.1f] %s" % (a, t) for a, t in segs]
        dest = self.whisper / f"{vid}.txt"
        if dest.exists() and self.has_transcript(vid):
            raise RuntimeError(f"{dest.name} already exists: refusing to overwrite a transcript")
        self.work.mkdir(parents=True, exist_ok=True)
        tmp = self.work / f"transcript_{vid}.part"
        tmp.write_bytes((nl.join(lines) + nl).encode("utf-8"))
        self.journal_add(dest)
        self.whisper.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dest)
        return dest

    def index_item(self, vid: str, title: str, upload_date: str, seconds: float) -> None:
        """Add one transcript to the repo's three Whisper indexes. Idempotent, and built from the transcript FILE,
        so a run that died between the transcript and its index entries can be finished later."""
        text = (self.whisper / f"{vid}.txt").read_text(encoding="utf-8")
        segs = [(float(m.group(1)), m.group(2)) for m in map(SEG.match, text.splitlines()[2:]) if m]
        nl = self.newline()
        # 1. transcripts/whisper/_index.jsonl : one JSON line per transcript, appended
        raw = self.index.read_bytes().decode("utf-8") if self.index.exists() else ""
        if not any(ln.strip() and json.loads(ln).get("id") == vid for ln in raw.splitlines()):
            line = json.dumps({"id": vid, "title": title, "upload_date": upload_date, "segments": len(segs),
                               "seconds": seconds})
            self.journal_add(self.index)
            with open(self.index, "ab") as fh:
                fh.write((("" if not raw or raw.endswith("\n") else nl) + line + nl).encode("utf-8"))
        # 2. chronology/records.json : compact JSON list sorted by id, one record with its segments per transcript
        if self.records.exists():
            raw_b = self.records.read_bytes()
            recs = json.loads(raw_b)
            dump = lambda x: json.dumps(x, ensure_ascii=False, separators=(",", ":")).encode("utf-8")  # noqa: E731
            if dump(recs) != raw_b:
                self.say("  ! chronology/records.json is not in the layout this tool writes: left untouched")
            elif not any(r.get("id") == vid for r in recs):
                d = upload_date
                date = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 and d.isdigit() else d
                recs.append({"id": vid, "title": title, "date": date, "source_type": "video", "url": WATCH % vid,
                             "segments": [{"t": int(a + 0.5), "text": t} for a, t in segs]})
                recs.sort(key=lambda r: r["id"])
                self.journal_add(self.records)
                tmp = self.records.with_suffix(".json.part")
                tmp.write_bytes(dump(recs))
                tmp.replace(self.records)
        # 3. chronology/playlist_ids_oldest_last.json : every video id, newest first, one per line
        if self.playlist.exists():
            raw_p = self.playlist.read_bytes().decode("utf-8")
            pnl = "\r\n" if "\r\n" in raw_p else "\n"
            ids = json.loads(raw_p)
            if json.dumps(ids, indent=0) != raw_p.replace("\r\n", "\n"):
                self.say("  ! chronology/playlist_ids_oldest_last.json is not in the layout this tool writes: "
                         "left untouched")
            elif vid not in ids:
                self.journal_add(self.playlist)
                tmp = self.playlist.with_suffix(".json.part")
                tmp.write_bytes(json.dumps([vid] + ids, indent=0).replace("\n", pnl).encode("utf-8"))
                tmp.replace(self.playlist)

    def record_failure(self, e: dict, reason: str) -> None:
        f = self.state["failed"].get(e["id"]) or {"n": 0}
        f.update(reason=reason[:240], n=f.get("n", 0) + 1, at=now_iso(), title=e.get("title", ""))
        self.state["failed"][e["id"]] = f
        self.save_state()
        self.summary["failed"].append((e["id"], reason))
        self.say(f"  ! {e['id']} failed ({f['n']}/{self.cfg['max_tries']}): {reason}")

    def process(self, e: dict) -> str:
        vid = e["id"]
        t0 = time.time()
        res = self.download(vid)                                 # BotCheck propagates: the run stops
        if res["kind"] == "live":
            self.summary["live"].append(vid)
            self.say(f"  ~ {vid} is live or still processing: skipped, it will be picked up when it is finished")
            return "live"
        if res["kind"] == "failed":
            self.record_failure(e, res["reason"])
            return "failed"
        meta = res["meta"]
        t1 = time.time()
        try:
            segs, seconds = self.transcribe(res["path"])
        except Exception as exc:                                 # one bad file must not stop the run
            self.record_failure(e, f"transcribe: {type(exc).__name__}: {exc}")
            return "failed"
        finally:
            self.clean_audio(vid)                                # the audio is worth keeping only as text
        title = meta.get("title") or e["title"]
        upload = meta.get("upload_date") or ""
        if not (len(upload) == 8 and upload.isdigit()):          # YouTube gave no date: fall back to the listing's
            upload = dt.datetime.fromtimestamp(e["ts"], dt.timezone.utc).strftime("%Y%m%d") if e.get("ts") else ""
        self.summary["audio_seconds"] += seconds
        entry = {"at": now_iso(), "title": title, "upload_date": upload, "seconds": seconds,
                 "segments": len(segs), "source": e.get("source", ""), "model": self.cfg["whisper_model"]}
        if not segs:                                             # a trailer with music only: nothing to archive
            entry["note"] = "no_speech"
            self.state["done"][vid] = entry
            self.state["failed"].pop(vid, None)
            self.save_state()
            self.summary["nospeech"].append(vid)
            self.say(f"  - {vid} has no speech: no transcript written ({title})")
            return "nospeech"
        self.state["in_progress"] = dict(entry, id=vid)
        self.save_state()
        self.write_transcript(vid, title, segs)
        self.index_item(vid, title, upload, seconds)
        self.state["done"][vid] = entry
        self.state["failed"].pop(vid, None)
        self.state.pop("in_progress", None)
        self.save_state()
        mins = (time.time() - t0) / 60
        self.summary["done"].append((vid, title, upload, seconds, mins))
        tsec = max(time.time() - t1, 0.001)
        self.say(f"  + {vid} {upload} {fmt_dur(seconds)} audio, {len(segs)} segments, {mins:.1f} min in all "
                 f"(download {t1 - t0:.0f} s, transcription {tsec:.0f} s = {seconds / tsec:.1f}x realtime): {title}")
        return "ok"

    def recover(self) -> None:
        """Finish (or forget) a video the previous run died on, and drop any audio it left behind."""
        self.clean_audio()
        ip = self.state.get("in_progress")
        if not ip:
            return
        vid = ip["id"]
        if self.has_transcript(vid):
            self.journal_add(self.whisper / f"{vid}.txt")
            self.index_item(vid, ip["title"], ip["upload_date"], ip["seconds"])
            self.state["done"][vid] = {k: v for k, v in ip.items() if k != "id"}
            self.summary["done"].append((vid, ip["title"], ip["upload_date"], ip["seconds"], 0.0))
            self.say(f"  + {vid} finished from the interrupted run (no new download)")
        else:
            self.say(f"  ~ {vid} was interrupted before its transcript was written: it will be done again")
        self.state.pop("in_progress", None)
        self.save_state()

    # ---- git --------------------------------------------------------------------------------------------------------
    def check_branch(self, branch: str) -> None:
        cur = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if cur != branch:
            raise Refused(f"the repository is on branch '{cur}', not '{branch}': not touching it "
                          f"(check out '{branch}', or pass --branch {cur})")
        gd = Path(self.git("rev-parse", "--absolute-git-dir").stdout.strip())
        if any((gd / n).exists() for n in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD")):
            raise Refused("a merge, rebase or cherry-pick is in progress in the repository: not touching it")

    def commit(self) -> str | None:
        """Commit exactly the files in the journal that differ from HEAD. Returns the new hash, or None."""
        paths = [p for p in self.journal() if (self.root / p).exists()]
        if self.cfg.get("rebuild_search_index") and self.summary["done"] and paths:
            r = subprocess.run([sys.executable, str(self.root / "tools" / "build_index.py")], cwd=self.root,
                               capture_output=True, text=True)
            if r.returncode == 0:
                paths.append(self.rel(self.search_index))
            else:
                self.say("  ! build_index.py failed, search index left as it was: " + (r.stderr or "")[-200:])
        changed = self.git("status", "--porcelain", "--", *paths).stdout.strip() if paths else ""
        if not changed:
            if self.journal_path.exists():
                self._save(self.journal_path, [])
            return None
        done, failed = self.summary["done"], self.summary["failed"]
        day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        subject = f"Video transcripts: {len(done)} new" + (f", {len(failed)} failed" if failed else "") + f" ({day})"
        body = [f"+ {d[:4]}-{d[4:6]}-{d[6:]} {vid} {title}" for vid, title, d, _s, _m in done]
        body += [f"! {vid} {reason[:100]}" for vid, reason in failed]
        body += [f"- {vid} no speech, no transcript" for vid in self.summary["nospeech"]]
        if not body:
            body = ["State from an interrupted run."]
        body.append(f"Whisper {self.cfg['whisper_model']} ({self.cfg['device']}/{self.cfg['compute_type']}), "
                    "audio only, by tools/collect_videos.py.")
        name, email = self.cfg["author_name"], self.cfg["author_email"]
        trailer = "Co-Authored-By: " + self.cfg["co_author"]
        self.git("add", "--", *paths)
        self.git("-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "--only",
                 f"--author={name} <{email}>", "-m", subject, "-m", "\n".join(body), "-m", trailer, "--", *paths)
        self._save(self.journal_path, [])
        head = self.git("rev-parse", "HEAD").stdout.strip()
        # Verify the COMMIT, not the command.
        got = sorted(x for x in self.git("show", "--name-only", "--format=", "HEAD").stdout.splitlines() if x)
        who = self.git("log", "-1", "--format=%an <%ae>").stdout.strip()
        trailers = [x for x in self.git("log", "-1", "--format=%(trailers:only,unfold)").stdout.splitlines() if x]
        problems = []
        if not set(got) <= set(paths):
            problems.append(f"files outside this tool's own: {sorted(set(got) - set(paths))}")
        if who != f"{name} <{email}>":
            problems.append(f"author is {who}")
        if trailers != [trailer]:
            problems.append(f"trailers are {trailers}")
        if problems:
            self.commit_problem = "; ".join(problems)
            self.say(f"  !! commit {head[:8]} is NOT as it should be: {self.commit_problem}. Nothing will be pushed.")
        else:
            self.say(f"committed {head[:8]}: {subject} ({len(got)} file(s))")
        return head

    commit_problem = ""

    def push(self, branch: str) -> bool:
        if not self.cfg.get("push"):
            return False
        remote = self.cfg["remote"]
        name, email = self.cfg["author_name"], self.cfg["author_email"]
        ident = ["-c", f"user.name={name}", "-c", f"user.email={email}"]
        self.git("fetch", "-q", remote, branch)
        ahead = self.git("rev-list", "--count", f"{remote}/{branch}..HEAD").stdout.strip()
        if ahead == "0":
            return False
        r = self.git(*ident, "rebase", "-q", "--autostash", f"{remote}/{branch}", check=False)
        if r.returncode != 0:
            self.git("rebase", "--abort", check=False)
            self.say(f"  ! could not rebase onto {remote}/{branch}; commits stay local: {r.stderr.strip()[:200]}")
            return False
        self.git("push", "-q", remote, f"HEAD:{branch}")
        self.say(f"pushed {ahead} commit(s) to {remote}/{branch}")
        return True

    # ---- a run ------------------------------------------------------------------------------------------------------
    def cooling_down(self) -> str:
        b = self.local.get("botcheck")
        if not b:
            return ""
        until = dt.datetime.strptime(b["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc) \
            + dt.timedelta(hours=float(self.cfg["botcheck_cooldown_hours"]))
        if dt.datetime.now(dt.timezone.utc) < until:
            return f"YouTube showed its bot check at {b['at']} ({b.get('during', '')}); not contacting it again " \
                   f"before {until.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        return ""

    def note_botcheck(self, exc: BotCheck) -> None:
        self.local["botcheck"] = {"at": now_iso(), "during": str(exc)[:300]}
        self.save_local()
        self.say(f"STOPPED: bot check / rate limit from YouTube ({exc}). Recorded; no retry until the cooldown "
                 f"({self.cfg['botcheck_cooldown_hours']} h) has passed.")

    def print_plan(self, plan: dict) -> None:
        take = plan["take"]
        print(f"\nWOULD DOWNLOAD (audio only) AND TRANSCRIBE: {len(take)} video(s), {plan['hours']:.2f} h of audio "
              f"(caps: {plan['max_items']} videos, {plan['cap']:g} h per run)")
        for e in take:
            when = dt.datetime.fromtimestamp(e["ts"], dt.timezone.utc).strftime("~%Y-%m") if e["ts"] else "   ?    "
            print(f"  {e['id']}  {fmt_dur(e['duration']):>8}  {when}  {e['source']:<16} {e['title']}")
        if take:
            print("WOULD ADD:")
            for e in take:
                print(f"  transcripts/whisper/{e['id']}.txt")
            print("WOULD CHANGE:")
            for p in (self.index, self.records, self.playlist):
                if p.exists() or p == self.index:
                    print(f"  {self.rel(p)}   (+{len(take)} entr{'y' if len(take) == 1 else 'ies'})")
            print(f"  {self.rel(self.state_path)}   ({'updated' if self.state_path.exists() else 'created'})")
            if self.cfg.get("rebuild_search_index"):
                print(f"  {self.rel(self.search_index)}   (rebuilt)")
            print(f"WOULD COMMIT as {self.cfg['author_name']} <{self.cfg['author_email']}>, trailer "
                  f"'Co-Authored-By: {self.cfg['co_author']}'; push: {'yes' if self.cfg.get('push') else 'NO'}")
        for label, key in (("LEFT FOR A LATER RUN", "deferred"),):
            if plan[key]:
                h = sum((e["duration"] or 0) for e, _ in plan[key]) / 3600
                print(f"{label}: {len(plan[key])} video(s), {h:.2f} h")
                for e, why in plan[key]:
                    print(f"  {e['id']}  {fmt_dur(e['duration']):>8}  {e['title']}  [{why}]")
        if plan["live"]:
            print(f"LIVE OR UPCOMING, SKIPPED: {len(plan['live'])}")
            for e in plan["live"]:
                print(f"  {e['id']}  {e['live_status']}  {e['title']}")
        if plan["gave_up"]:
            print(f"FAILED {self.cfg['max_tries']} TIMES, LEFT ALONE: {len(plan['gave_up'])}")
            for e in plan["gave_up"]:
                print(f"  {e['id']}  {e['title']}  [{self.state['failed'][e['id']].get('reason', '')}]")
        if plan["gaps"]:
            h = sum((e["duration"] or 0) for e in plan["gaps"]) / 3600
            print(f"OLDER GAPS (published before {self.cfg['not_before']}, not in the corpus, NOT taken without "
                  f"--backfill): {len(plan['gaps'])} video(s), {h:.2f} h")
            for e in plan["gaps"]:
                print(f"  {e['id']}  {fmt_dur(e['duration']):>8}  {e['source']:<16} {e['title']}")

    def run(self, limit=None, hours=None, backfill=False, branch=None, ignore_cooldown=False) -> int:
        cfg = self.cfg
        branch = branch or cfg["branch"]
        rc = 0
        if not self.dry:
            self.check_branch(branch)
            self.work.mkdir(parents=True, exist_ok=True)
            (self.work / ".gitignore").write_text("*\n", encoding="utf-8")
            try:                            # the log never grows past a few MB: keep its newest megabyte
                if self.log_path.stat().st_size > 3_000_000:
                    self.log_path.write_bytes(self.log_path.read_bytes()[-1_000_000:])
            except OSError:
                pass
            self.recover()
        cool = "" if ignore_cooldown else self.cooling_down()
        if cool:
            self.say("Not running: " + cool)
            rc = 3
        else:
            try:
                rc = self._collect(limit, hours, backfill)
            except BotCheck as exc:
                if self.dry:
                    print(f"STOPPED: bot check / rate limit from YouTube ({exc}). (dry run: not recorded)")
                else:
                    self.note_botcheck(exc)
                rc = 3
            except Refused as exc:
                self.say(f"Stopped: {exc}")
                rc = 2
            except Exception as exc:        # no network at logon, YouTube changed a page... say so and end cleanly;
                self.say(f"Stopped by an error: {type(exc).__name__}: {exc}")   # what is already written is kept
                rc = 1
        if self.dry:
            return rc
        try:
            self.commit()
            if self.commit_problem:
                return 5
            self.push(branch)
        finally:
            self.local["last_run"] = {"at": now_iso(), "rc": rc, "done": len(self.summary["done"]),
                                      "failed": len(self.summary["failed"]),
                                      "audio_hours": round(self.summary["audio_seconds"] / 3600, 3)}
            self.save_local()
        return rc

    def _collect(self, limit, hours, backfill) -> int:
        cfg = self.cfg
        sources = [s for s in cfg["sources"] if s.get("enabled", True)]
        by_source = []
        for i, src in enumerate(sources):
            if i:
                self.sleep(3)
            entries = self.list_source(src)
            self.say(f"{src['name']}: newest {len(entries)} listed, "
                     f"{sum(1 for e in entries if not self.is_known(e['id']))} not in the corpus")
            by_source.append(entries)
        plan = self.plan(by_source, limit, hours, backfill)
        if self.dry:
            self.print_plan(plan)
            return 0
        if not plan["take"]:
            self.say(f"nothing new ({len(plan['deferred'])} deferred, {len(plan['live'])} live, "
                     f"{len(plan['gaps'])} older gaps)")
            return 0
        if cfg.get("below_normal_priority", True):
            lower_priority()
        cap_seconds = plan["cap"] * 3600
        attempts = 0
        try:
            for e in plan["take"]:
                if attempts >= plan["max_items"]:
                    break
                if self.summary["audio_seconds"] + (e["duration"] or 0) > cap_seconds:
                    self.say(f"  ~ {e['id']} left for the next run: the audio-hours cap is reached")
                    continue
                free = shutil.disk_usage(self.root).free / 2**30
                if free < float(cfg["min_free_disk_gb"]):
                    raise Refused(f"only {free:.1f} GB free on the disk (min_free_disk_gb {cfg['min_free_disk_gb']})")
                if attempts:
                    self.sleep(float(cfg["pause_seconds"]) * random.uniform(0.8, 1.2))
                attempts += 1
                self.say(f"{e['id']} {fmt_dur(e['duration'])} {e['title']}")
                self.process(e)
        finally:
            self.clean_audio()
        s = self.summary
        self.say(f"run: {len(s['done'])} transcribed ({s['audio_seconds'] / 3600:.2f} h audio), {len(s['failed'])} "
                 f"failed, {len(s['live'])} live, {len(s['nospeech'])} without speech, "
                 f"{len(plan['deferred'])} left for later")
        return 1 if s["failed"] else 0


# ---- selftest (no network, no Whisper: a fake YouTube and throwaway git repositories) ---------------------------------
BOT_TEXT = ("ERROR: [youtube] %s: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies "
            "for the authentication. See https://github.com/yt-dlp/yt-dlp/wiki/FAQ for how to manually pass cookies.")
E403_TEXT = "ERROR: unable to download video data: HTTP Error 403: Forbidden"
T_AUTHOR = "ElahMoth <322847132+elahmoth@users.noreply.github.com>"
T_TRAILER = "Co-Authored-By: ScPlaceholder <starcitizenplaceholder@gmail.com>"


class FakeTube:
    """Stands in for yt-dlp: answers the listing and download command lines the Collector really builds."""

    def __init__(self, listings: dict, behave: dict | None = None):
        self.listings, self.behave, self.calls = listings, behave or {}, []

    @property
    def downloads(self) -> list[str]:
        return [c[-1].split("v=")[1] for c in self.calls if "--flat-playlist" not in c]

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if "--flat-playlist" in args:
            if self.behave.get("LISTING") == "botcheck":
                return 1, "", BOT_TEXT % "tab"
            entries = self.listings.get(args[-1], [])[: int(args[args.index("--playlist-end") + 1])]
            return 0, json.dumps({"entries": entries}), ""
        vid = args[-1].split("v=")[1]
        how = self.behave.get(vid, "ok")
        meta = f"META\t{vid}\t20261001\t20\t{'is_live' if how == 'live_late' else 'not_live'}\tTitle of {vid}\n"
        if how == "botcheck":
            return 1, "", BOT_TEXT % vid
        if how == "403":
            return 1, meta, E403_TEXT
        if how == "live_late":
            return 0, meta, ""
        Path(args[args.index("-o") + 1].replace("%(ext)s", "webm")).write_bytes(b"not really audio")
        return 0, meta, ""


def _selftest() -> int:
    import contextlib
    import io
    import stat
    import tempfile

    results = []
    V, S = DEFAULTS["sources"][0]["url"], DEFAULTS["sources"][1]["url"]
    NOW = int(time.time())
    OLD = NOW - 400 * 86400

    def entry(vid, dur=600, live=None, ts=NOW):
        return {"id": vid, "title": f"Title of {vid}", "duration": dur, "live_status": live, "timestamp": ts}

    def sh(root, *args):
        r = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        assert r.returncode == 0, (args, r.stderr)
        return r.stdout.strip()

    def make_repo(tmp: Path) -> Path:
        root = tmp / "repo"
        for d in ("transcripts/whisper", "transcripts/captions", "chronology", "tools"):
            (root / d).mkdir(parents=True)
        (root / "transcripts/whisper/OLDwhisper1.txt").write_bytes(
            b"# Old one\n# https://www.youtube.com/watch?v=OLDwhisper1\n[    1.0] Hello.\n")
        (root / "transcripts/captions/OLDcaption1.txt").write_bytes(b"hello from captions\n")
        (root / "transcripts/whisper/_index.jsonl").write_bytes((json.dumps(
            {"id": "OLDwhisper1", "title": "Old one", "upload_date": "20250101", "segments": 1, "seconds": 3.0})
            + "\n").encode())
        (root / "chronology/records.json").write_bytes(json.dumps(
            [{"id": "OLDwhisper1", "title": "Old one ’quoted’", "date": "2025-01-01", "source_type": "video",
              "url": WATCH % "OLDwhisper1", "segments": [{"t": 1, "text": "Hello."}]}],
            ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        (root / "chronology/playlist_ids_oldest_last.json").write_bytes(
            json.dumps(["OLDwhisper1", "OLDcaption1"], indent=0).encode())
        (root / "NOTICE").write_bytes(b"notice\n")
        (root / "README.md").write_bytes(b"readme\n")
        sh(tmp, "init", "-q", "--bare", "remote.git")
        sh(root, "init", "-q", "-b", "corpus")
        sh(root, "config", "core.autocrlf", "false")
        sh(root, "add", "-A")
        sh(root, "-c", "user.name=Seed", "-c", "user.email=seed@example.invalid", "commit", "-q", "-m", "seed")
        sh(root, "remote", "add", "origin", str(tmp / "remote.git"))
        sh(root, "push", "-q", "origin", "corpus")
        return root

    def collector(root, tube, dry=False, **over):
        cfg = json.loads(json.dumps(DEFAULTS))
        cfg.update(pause_seconds=0, min_free_disk_gb=0)
        cfg.update(over)
        c = Collector(root, cfg, dry=dry)
        c.ytdlp = tube
        c.transcribe = lambda p: ([(0.24, "Hello citizens."), (5.5, "Second line."), (12.04, "Third.")], 20.5)
        c.sleeps = []
        c.sleep = c.sleeps.append
        return c

    def snapshot(root):
        out = {}
        for p in sorted(root.rglob("*")):
            if p.is_file():
                out[p.relative_to(root).as_posix()] = p.read_bytes()
        return out

    def case(name):
        def deco(fn):
            tmp = Path(tempfile.mkdtemp(prefix="scv_selftest_"))
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    fn(make_repo(tmp), tmp)
                results.append((name, ""))
            except AssertionError as exc:
                results.append((name, "assertion: " + (str(exc) or "(no message)")[:300]))
            except Exception as exc:                          # a crash is a failure, and says it is a crash
                results.append((name, f"CRASH {type(exc).__name__}: {exc}"[:300]))
            finally:
                def _rw(func, path, _exc):
                    os.chmod(path, stat.S_IWRITE)
                    func(path)
                shutil.rmtree(tmp, onerror=_rw)
            return fn
        return deco

    @case("1 known ids are skipped: in the state file, already transcribed (whisper or captions), given up")
    def _(root, tmp):
        tube = FakeTube({V: [entry("NEWvideo001"), entry("DONEinstate"), entry("GAVEUP00001"), entry("OLDwhisper1"),
                             entry("OLDcaption1")]})
        c = collector(root, tube)
        c.state["done"]["DONEinstate"] = {"at": "x", "title": "t"}
        c.state["failed"]["GAVEUP00001"] = {"n": 3, "reason": "http_403: x"}
        assert c.run() == 0
        assert tube.downloads == ["NEWvideo001"], tube.downloads

    @case("2 a bot check stops the run, is recorded, fails nothing, and the next run stays off YouTube")
    def _(root, tmp):
        tube = FakeTube({V: [entry("CCCCCCCCCCC"), entry("BBBBBBBBBBB"), entry("AAAAAAAAAAA")]},
                        {"BBBBBBBBBBB": "botcheck"})
        c = collector(root, tube)
        assert c.run() == 3, "exit code is not 3"
        assert tube.downloads == ["AAAAAAAAAAA", "BBBBBBBBBBB"], f"the run went on after the check: {tube.downloads}"
        assert "BBBBBBBBBBB" not in c.state["failed"], "the bot-checked video was marked failed"
        local = json.loads((root / "_video_work/local_state.json").read_text(encoding="utf-8"))
        assert "not a bot" in local["botcheck"]["during"], "bot check not recorded"
        assert "AAAAAAAAAAA" in c.state["done"] and (root / "transcripts/whisper/AAAAAAAAAAA.txt").exists()
        tube2 = FakeTube({V: [entry("CCCCCCCCCCC"), entry("BBBBBBBBBBB")]})
        c2 = collector(root, tube2)
        assert c2.run() == 3 and tube2.calls == [], f"contacted YouTube during the cooldown: {tube2.calls}"
        tube3 = FakeTube({V: [entry("NEWvideo001")]}, {"LISTING": "botcheck"})
        c3 = collector(root, tube3, botcheck_cooldown_hours=0)
        assert c3.run() == 3 and tube3.downloads == [], "a bot check on the listing did not stop the run"

    @case("3 an HTTP 403 is recorded as failed with its reason and the run continues")
    def _(root, tmp):
        tube = FakeTube({V: [entry("BBBBBBBBBBB"), entry("AAAAAAAAAAA")]}, {"AAAAAAAAAAA": "403"})
        c = collector(root, tube)
        assert c.run() == 1, "exit code is not 1"
        st = json.loads((root / "chronology/video_state.json").read_text(encoding="utf-8"))
        f = st["failed"].get("AAAAAAAAAAA") or {}
        assert f.get("n") == 1 and "403" in f.get("reason", ""), f"403 not recorded: {f}"
        assert tube.downloads == ["AAAAAAAAAAA", "BBBBBBBBBBB"], f"the run did not continue: {tube.downloads}"
        assert "BBBBBBBBBBB" in st["done"] and not (root / "transcripts/whisper/AAAAAAAAAAA.txt").exists()
        assert not list((root / "_video_work").glob("audio_*")), "audio left behind"

    @case("4 live, upcoming and just-ended items are skipped and not remembered")
    def _(root, tmp):
        tube = FakeTube({V: [entry("UPCOMING001", live="is_upcoming"), entry("LIVENOW0001", live="is_live"),
                             entry("POSTLIVE001", live="post_live"), entry("LATELIVE001"), entry("WASLIVE0001",
                                                                                                   live="was_live")]},
                        {"LATELIVE001": "live_late"})
        c = collector(root, tube)
        assert c.run() == 0
        assert tube.downloads == ["WASLIVE0001", "LATELIVE001"], f"downloads: {tube.downloads}"
        assert set(c.state["done"]) == {"WASLIVE0001"} and not c.state["failed"], (c.state["done"], c.state["failed"])
        assert not (root / "transcripts/whisper/LATELIVE001.txt").exists(), "a live item was transcribed"

    @case("5 the caps are honoured: videos per run, audio hours per run, --limit, pause between downloads")
    def _(root, tmp):
        five = [entry(f"ITEM000000{i}") for i in (5, 4, 3, 2, 1)]
        tube = FakeTube({V: five})
        c = collector(root, tube, max_items_per_run=2, pause_seconds=10)
        assert c.run() == 0
        assert tube.downloads == ["ITEM0000001", "ITEM0000002"], f"item cap: {tube.downloads}"
        assert len(c.sleeps) == 2 and c.sleeps[0] == 3 and 8 <= c.sleeps[1] <= 12, f"pauses (sources, downloads): {c.sleeps}"
        tube = FakeTube({V: five})
        c = collector(root, tube, max_items_per_run=4)
        assert c.run(limit=1) == 0 and tube.downloads == ["ITEM0000003"], f"--limit: {tube.downloads}"
        tube = FakeTube({V: [entry("HOUR0000003", dur=3600), entry("HUGE0000001", dur=9 * 3600),
                             entry("HOUR0000002", dur=3600), entry("HOUR0000001", dur=3600)]})
        c = collector(root, tube, max_items_per_run=10, max_audio_hours_per_run=2.5)
        assert c.run() == 0
        assert tube.downloads == ["HOUR0000001", "HOUR0000002"], f"hours cap: {tube.downloads}"
        tube = FakeTube({V: [entry("FAILS000003"), entry("FAILS000002"), entry("FAILS000001")]},
                        {"FAILS000001": "403", "FAILS000002": "403", "FAILS000003": "403"})
        c = collector(root, tube, max_items_per_run=2)
        c.run()
        assert len(tube.downloads) == 2, f"failed attempts must count against the item cap: {tube.downloads}"

    @case("6 transcript, the three indexes and the state are written in the repo's exact format; audio deleted")
    def _(root, tmp):
        before = snapshot(root)
        c = collector(root, FakeTube({V: [entry("NEWvideo001")]}))
        assert c.run() == 0
        t = (root / "transcripts/whisper/NEWvideo001.txt").read_bytes()
        assert t == (b"# Title of NEWvideo001\n# https://www.youtube.com/watch?v=NEWvideo001\n"
                     b"[    0.2] Hello citizens.\n[    5.5] Second line.\n[   12.0] Third.\n"), t
        idx = (root / "transcripts/whisper/_index.jsonl").read_bytes()
        assert idx.startswith(before["transcripts/whisper/_index.jsonl"]), "existing index lines changed"
        assert json.loads(idx.decode().splitlines()[-1]) == {"id": "NEWvideo001", "title": "Title of NEWvideo001",
                                                             "upload_date": "20261001", "segments": 3,
                                                             "seconds": 20.5}, idx
        recs = json.loads((root / "chronology/records.json").read_bytes())
        assert [r["id"] for r in recs] == ["NEWvideo001", "OLDwhisper1"], "records not sorted by id"
        assert recs[0] == {"id": "NEWvideo001", "title": "Title of NEWvideo001", "date": "2026-10-01",
                           "source_type": "video", "url": WATCH % "NEWvideo001",
                           "segments": [{"t": 0, "text": "Hello citizens."}, {"t": 6, "text": "Second line."},
                                        {"t": 12, "text": "Third."}]}, recs[0]
        assert before["chronology/records.json"][1:-1] in (root / "chronology/records.json").read_bytes(), \
            "the existing record was rewritten"
        assert (root / "chronology/playlist_ids_oldest_last.json").read_bytes() == \
            b'[\n"NEWvideo001",\n"OLDwhisper1",\n"OLDcaption1"\n]', "playlist"
        assert (root / "transcripts/whisper/OLDwhisper1.txt").read_bytes() == \
            before["transcripts/whisper/OLDwhisper1.txt"], "an existing transcript changed"
        assert not list((root / "_video_work").glob("audio_*")), "audio left behind"
        assert sh(root, "status", "--porcelain") == "", "work files are not ignored or something is uncommitted"

    @case("7 the commit stages only its own files and carries the author and the single trailer")
    def _(root, tmp):
        (root / "NOTICE").write_bytes(b"someone else's staged edit\n")
        sh(root, "add", "NOTICE")
        (root / "README.md").write_bytes(b"someone else's unstaged edit\n")
        (root / "stray.txt").write_bytes(b"untracked\n")
        (root / "transcripts/whisper/STRAYnotours.txt").write_bytes(b"# not ours\n# x\n[    1.0] x\n")
        c = collector(root, FakeTube({V: [entry("NEWvideo001")]}))
        assert c.run() == 0, "run failed"
        files = sorted(sh(root, "show", "--name-only", "--format=", "HEAD").splitlines())
        assert files == ["chronology/playlist_ids_oldest_last.json", "chronology/records.json",
                         "chronology/video_state.json", "transcripts/whisper/NEWvideo001.txt",
                         "transcripts/whisper/_index.jsonl"], f"commit holds {files}"
        assert sh(root, "log", "-1", "--format=%an <%ae>") == T_AUTHOR, sh(root, "log", "-1", "--format=%an <%ae>")
        assert sh(root, "log", "-1", "--format=%cn <%ce>") == T_AUTHOR, "committer differs"
        tr = sh(root, "log", "-1", "--format=%(trailers:only,unfold)").splitlines()
        assert tr == [T_TRAILER], f"trailers: {tr}"
        msg = sh(root, "log", "-1", "--format=%B")
        assert msg.rstrip().splitlines()[-1] == T_TRAILER, "the trailer is not the last line"
        assert sh(root, "diff", "--cached", "--name-only") == "NOTICE", "the other staged file was disturbed"
        assert "stray.txt" in sh(root, "status", "--porcelain") and "STRAYnotours" in sh(root, "status", "--porcelain")
        assert not any(a and a[0] == "add" and ("-A" in a or "." in a or "--all" in a) for a in c.git_calls)

    @case("8 nothing new means no commit, and a second run does nothing twice")
    def _(root, tmp):
        head = sh(root, "rev-parse", "HEAD")
        c = collector(root, FakeTube({V: [entry("OLDwhisper1")]}))
        assert c.run() == 0 and sh(root, "rev-parse", "HEAD") == head, "committed with nothing new"
        assert not any(a and a[0] == "commit" or "commit" in a for a in c.git_calls), "a commit was attempted"
        tube = FakeTube({V: [entry("NEWvideo001")]})
        assert collector(root, tube).run() == 0
        head = sh(root, "rev-parse", "HEAD")
        tube = FakeTube({V: [entry("NEWvideo001")]})
        c = collector(root, tube)
        assert c.run() == 0 and tube.downloads == [] and sh(root, "rev-parse", "HEAD") == head, "done twice"

    @case("9 push false: no push is attempted and the remote does not move; push true: it does")
    def _(root, tmp):
        remote_before = sh(tmp / "remote.git", "rev-parse", "corpus")
        c = collector(root, FakeTube({V: [entry("NEWvideo001")]}))
        assert c.run() == 0
        assert sh(root, "rev-parse", "HEAD") != remote_before, "no commit was made"
        assert not any("push" in a or "fetch" in a for a in c.git_calls), f"network git call with push off"
        assert sh(tmp / "remote.git", "rev-parse", "corpus") == remote_before, "the remote moved with push off"
        c = collector(root, FakeTube({V: [entry("NEWvideo002"), entry("NEWvideo001")]}), push=True)
        assert c.run() == 0
        assert sh(tmp / "remote.git", "rev-parse", "corpus") == sh(root, "rev-parse", "HEAD"), "push true did not push"

    @case("10 a second instance refuses to start while one is running")
    def _(root, tmp):
        lock = acquire_lock(root / "_video_work" / "collect_videos.lock")
        assert lock is not None, "could not take the lock at all"
        cmd = [sys.executable, str(Path(__file__).resolve()), "--root", str(root), "--lock-probe"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            assert r.returncode == 4, f"second instance exit code {r.returncode}: {r.stdout} {r.stderr}"
        finally:
            lock.close()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"lock not released: {r.returncode} {r.stdout} {r.stderr}"

    @case("11 --dry-run lists the plan and touches nothing")
    def _(root, tmp):
        before = snapshot(root)
        head = sh(root, "rev-parse", "HEAD")
        tube = FakeTube({V: [entry("NEWvideo002"), entry("NEWvideo001")], S: [entry("LIVENOW0001", live="is_live")]})
        c = collector(root, tube, dry=True)
        assert c.run() == 0
        assert tube.downloads == [], "a dry run downloaded"
        assert snapshot(root) == before, "a dry run wrote or changed a file"
        assert sh(root, "rev-parse", "HEAD") == head and not any("commit" in a or "add" in a for a in c.git_calls)

    @case("12 only what is published on or after not_before is new; older unknown videos are gaps unless --backfill")
    def _(root, tmp):
        listing = {V: [entry("NEWvideo001"), entry("OLDwhisper1"), entry("OLDGAP00001", ts=OLD)],
                   S: [entry("OLDSTREAM01", ts=OLD, live="was_live"), entry("NODATE00001", ts=None)]}
        tube = FakeTube(listing)
        c = collector(root, tube, not_before=(dt.date.today() - dt.timedelta(days=30)).isoformat())
        assert c.run() == 0
        assert tube.downloads == ["NEWvideo001", "NODATE00001"], f"new: {tube.downloads}"
        tube = FakeTube(listing)
        c = collector(root, tube, not_before=(dt.date.today() - dt.timedelta(days=30)).isoformat())
        assert c.run(backfill=True) == 0
        assert sorted(tube.downloads) == ["OLDGAP00001", "OLDSTREAM01"], f"backfill: {tube.downloads}"

    @case("13 an interrupted video is finished from its transcript without a second download; wrong branch refused")
    def _(root, tmp):
        c = collector(root, FakeTube({V: []}))
        c.work.mkdir(parents=True, exist_ok=True)
        c.state["in_progress"] = {"id": "CRASHED0001", "at": "x", "title": "Crashed", "upload_date": "20260930",
                                  "seconds": 9.0, "segments": 1, "source": "youtube-videos", "model": "small"}
        c.save_state()
        (root / "transcripts/whisper/CRASHED0001.txt").write_bytes(
            b"# Crashed\n# https://www.youtube.com/watch?v=CRASHED0001\n[    2.5] Only line.\n")
        (root / "_video_work/audio_LEFTOVER.webm").write_bytes(b"x")
        tube = FakeTube({V: [entry("CRASHED0001")]})
        c = collector(root, tube)
        assert c.run() == 0 and tube.downloads == [], "downloaded again"
        assert "CRASHED0001" in c.state["done"] and "in_progress" not in c.state
        assert '"id": "CRASHED0001"' in (root / "transcripts/whisper/_index.jsonl").read_text(encoding="utf-8")
        assert json.loads((root / "chronology/records.json").read_bytes())[0]["segments"] == [{"t": 3, "text":
                                                                                                "Only line."}]
        assert "transcripts/whisper/CRASHED0001.txt" in sh(root, "show", "--name-only", "--format=", "HEAD")
        assert not (root / "_video_work/audio_LEFTOVER.webm").exists(), "stale audio kept"
        sh(root, "checkout", "-q", "-b", "elsewhere")
        head = sh(root, "rev-parse", "HEAD")
        tube = FakeTube({V: [entry("NEWvideo001")]})
        c = collector(root, tube)
        try:
            c.run()
            raise AssertionError("ran on the wrong branch")
        except Refused:
            pass
        assert tube.calls == [] and sh(root, "rev-parse", "HEAD") == head

    @case("14 yt-dlp errors are told apart: bot check, 429, 403, age gate, timeout")
    def _(root, tmp):
        assert classify(BOT_TEXT % "x")[0] == "botcheck"
        assert classify("ERROR: [youtube] x: Sign in to confirm you're not a bot.")[0] == "botcheck"
        assert classify("ERROR: unable to download webpage: HTTP Error 429: Too Many Requests")[0] == "botcheck"
        assert classify(E403_TEXT) == ("failed", "http_403: " + E403_TEXT)
        assert classify("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate")[0] == \
            "failed"
        assert classify("ERROR: TIMEOUT after 5s")[1].startswith("timeout")

    @case("15 a title with a curly apostrophe survives the trip from the yt-dlp child process into the files")
    def _(root, tmp):
        curly = "Grey" + chr(0x2019) + "s Market Basher"
        rc, out, _err = run_child([sys.executable, "-c", "print('Grey' + chr(0x2019) + 's Market Basher')"], 60)
        assert rc == 0 and out.strip() == curly, f"the child's output arrived as {out.strip()!a}"
        tube = FakeTube({V: [entry("NEWvideo001")]})

        def with_curly(args, timeout):
            rc, out, err = tube(args, timeout)
            return rc, out.replace("Title of NEWvideo001", curly), err
        c = collector(root, with_curly)
        assert c.run() == 0
        head = (root / "transcripts/whisper/NEWvideo001.txt").read_bytes().split(b"\n")[0].decode("utf-8")
        assert head == "# " + curly, f"transcript header is {head!a}"
        assert json.loads((root / "chronology/records.json").read_bytes())[0]["title"] == curly, "records title"

    @case("16 a listing that fails for another reason ends the run with exit 1, no crash, nothing recorded as a bot check")
    def _(root, tmp):
        def offline(args, timeout):
            return 1, "", "ERROR: Unable to download API page: <urlopen error [Errno 11001] getaddrinfo failed>"
        c = collector(root, offline)
        assert c.run() == 1, "exit code is not 1"
        assert "botcheck" not in c.local and not c.state["failed"], "an outage was recorded as something else"
        assert "getaddrinfo" in (root / "_video_work/collect_videos.log").read_text(encoding="utf-8"), "not logged"

    width = max(len(n) for n, _ in results)
    for name, problem in results:
        print(("  PASS  " if not problem else "  FAIL  ") + name.ljust(width) + ("" if not problem else "\n          "
                                                                                + problem))
    bad = sum(1 for _, p in results if p)
    print(f"collect_videos selftest: {'PASS' if not bad else 'FAIL'} ({len(results) - bad}/{len(results)})")
    return 0 if not bad else 1


def main(argv=None) -> int:
    for name in ("stdout", "stderr"):                 # pythonw.exe (the hidden scheduled run) has neither
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        try:
            getattr(sys, name).reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Transcribe CIG's new videos and finished streams into the corpus.")
    ap.add_argument("--dry-run", action="store_true", help="list what a run would do; touch nothing")
    ap.add_argument("--limit", type=int, help="at most N videos this run (instead of max_items_per_run)")
    ap.add_argument("--hours", type=float, help="audio-hours cap for this run (instead of the config's)")
    ap.add_argument("--backfill", action="store_true", help="also take unknown videos older than not_before")
    ap.add_argument("--branch", help="the branch that must be checked out (instead of the config's)")
    ap.add_argument("--config", type=Path, default=CONFIG)
    ap.add_argument("--ignore-cooldown", action="store_true", help="run even though a bot check is cooling down")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    ap.add_argument("--lock-probe", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    try:
        cfg = load_config(a.config)
        dt.date.fromisoformat(cfg["not_before"])
    except (ValueError, KeyError) as exc:
        print(f"bad config {a.config}: {exc}")
        return 2
    if a.dry_run:
        return Collector(a.root, cfg, dry=True).run(a.limit, a.hours, a.backfill, a.branch, a.ignore_cooldown)
    lock = acquire_lock(a.root / "_video_work" / "collect_videos.lock")
    if lock is None:
        print("another collect_videos.py is running: not starting a second one")
        return 4
    try:
        if a.lock_probe:
            return 0
        c = Collector(a.root, cfg)
        c.say(f"--- run start (limit {a.limit}, hours {a.hours}, backfill {a.backfill})")
        try:
            return c.run(a.limit, a.hours, a.backfill, a.branch, a.ignore_cooldown)
        except Refused as exc:
            c.say(f"Refused: {exc}")
            return 2
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
