# SC Dev History

A searchable, dated archive of Star Citizen's public development history: transcripts of Cloud Imperium Games'
development videos (Inside Star Citizen, Star Citizen Live, Reverse the Verse, Calling All Devs and more), RSI
comm-links (including every Monthly Report, in full) and CIG developer posts from the Spectrum Devtracker, lined up on
one timeline from 2012 to today.

> **Unofficial fan transcripts.** Star Citizen, Squadron 42, Roberts Space Industries and all related content are
> © Cloud Imperium Games. This project is not affiliated with or endorsed by Cloud Imperium Games. We claim no
> copyright over any of it. The transcripts are provided free, for non-commercial searching and reference, and every
> entry points back to the original video or comm-link.
>
> **Takedown:** if you are Cloud Imperium Games (or anyone with rights in this content) and want something removed,
> open an issue or contact the maintainer and it will be removed, no questions asked.

## What is here

| Path | What |
|---|---|
| `transcripts/captions/<video_id>.txt` | Transcripts from YouTube caption tracks, one file per video |
| `transcripts/whisper/<video_id>.txt` | Transcripts made with Whisper for videos without usable captions |
| `transcripts/whisper/_index.jsonl` | Title and upload date for each Whisper transcript |
| `chronology/chronology_meta.json` | Video id → date, title, episode, ordering |
| `chronology/playlist_ids_oldest_last.json` | The full video list, oldest last |
| `chronology/records.json` | Dated, searchable records built from the transcripts |
| `chronology/web_records.json` | RSI comm-link records: title, date, URL, short summary (2012-09-12 onward) |
| `chronology/commlink_index.json` | The raw comm-link archive index (newest first, as RSI lists it) |
| `commlinks/<id>.txt` | Full text of a comm-link (`<id>` is the number in its URL); `## ` marks a heading. Its record in `web_records.json` gets a `digest`: an extractive summary of what it says (the opening plus one line per section) |
| `chronology/commlink_bodies.json` | Which comm-link bodies are fetched, empty, or failed (so runs resume) |
| `chronology/devtracker.json` | CIG posts from the Spectrum Devtracker: author, forum, thread, date, teaser, link |
| `devposts/<id>.txt` | Full text of a Devtracker post (public forums only) |
| `chronology/devtracker_state.json` | Devtracker backfill position and body status |
| `chronology/search_index.json.gz` | The compact search index SC Toolbox downloads (built by `tools/build_index.py`) |
| `chronology/title_patch.json` | Titles recovered for videos missing from the original metadata |
| `chronology/video_state.json` | Which new videos `tools/collect_videos.py` has transcribed, and which failed and why (so runs resume) |

Transcripts are machine-produced (auto-captions or Whisper) and will contain recognition errors, especially in names
and ship designations. When it matters, check the video.

## Keeping it current

`.github/workflows/update-corpus.yml` runs daily. It adds new comm-links and Devtracker posts, fetches a bounded
number of full texts per run (so the backfill of older articles spreads over days and RSI never notices), rebuilds
the search index and commits. Everything is incremental and resumable. Run it by hand from the Actions tab
("Run workflow") with larger budgets to speed up the backfill.

    python tools/rsi.py --selftest            # parsers, against saved shapes of RSI's pages (no network)
    python tools/test_collectors.py           # both collectors end to end against a fake RSI (no network)
    python tools/collect_commlinks.py --probe # live check: 1 listing page + 1 article body, writes nothing
    python tools/collect_spectrum.py --probe  # live check: 1 Devtracker page + 1 post body, writes nothing

If RSI blocks GitHub's runners, or you fetched things in SC Toolbox (Dev History's "Load full history" / "Download
all"), bring them in from your PC:

    python tools/import_local_cache.py    # from ~/.sctoolbox/dev_history/live; then build_index.py, commit, push

Posts in private Spectrum forums (Focus Testing, Evocati and the like) keep only the teaser the public Devtracker
shows; the collector never tries to get past a permission check. Video transcripts are not collected by this
workflow: see the next section.

## New videos and streams (runs on a PC)

`tools/collect_videos.py` finds CIG's new videos and finished live streams on the official YouTube channel,
downloads the audio track only, transcribes it locally with Whisper (faster-whisper, on the CPU, at below-normal
priority), writes `transcripts/whisper/<video_id>.txt`, adds it to `transcripts/whisper/_index.jsonl`,
`chronology/records.json` and `chronology/playlist_ids_oldest_last.json`, deletes the audio and commits exactly the
files it wrote. The daily workflow above then rebuilds the search index. It cannot run in GitHub Actions: YouTube
blocks datacenter addresses, and it needs a local Whisper (`pip install yt-dlp faster-whisper`).

    python tools/collect_videos.py --dry-run     # what a run would download, add and change; touches nothing
    python tools/collect_videos.py --limit 2     # a real run, at most 2 videos
    python tools/collect_videos.py --selftest    # every rule against a fake YouTube, no network

Everything adjustable is in `tools/collect_videos.json`: the sources, the date before which nothing is taken
automatically, the caps per run (videos and audio hours), the pause between downloads, the Whisper model, and
`"push"`, which is `false`: commits stay local until you push them or set it to `true`. It works one video at a
time, skips anything still live, and stops for the day if YouTube shows its "confirm you're not a bot" check. A
video whose download fails is recorded with the reason in `chronology/video_state.json` and retried on the next
runs, up to three times. Measured on the maintainer's PC (2026-10-06, `small` model, int8, 4 CPU threads): an
8-minute video transcribed in 47 seconds, about 10.7x realtime, so a two-hour Star Citizen Live takes roughly 11
minutes of below-normal-priority CPU.

To run it by itself on Windows (a few minutes after logon, then every 6 hours, hidden, never two at once):

    powershell -ExecutionPolicy Bypass -File tools\register_video_task.ps1 -Show   # print the task, register nothing
    powershell -ExecutionPolicy Bypass -File tools\register_video_task.ps1
    powershell -ExecutionPolicy Bypass -File tools\unregister_video_task.ps1

## How it is used

[SC Toolbox](https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2) streams this repository rather than bundling it:
the tool downloads the small index once and fetches an individual transcript or article only when you open a result,
from `raw.githubusercontent.com`.

To watch the source of any transcript: `https://www.youtube.com/watch?v=<video_id>` (the file name is the id).
