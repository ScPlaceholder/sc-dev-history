# SC Dev History

A searchable, dated archive of Star Citizen's public development history: transcripts of Cloud Imperium Games'
development videos (Inside Star Citizen, Star Citizen Live, Reverse the Verse, Calling All Devs and more) and an
index of RSI comm-links, lined up on one timeline from 2012 to today.

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
| `chronology/web_records.json` | 5,152 RSI comm-link records: title, date, URL, short summary (2012-09-12 onward) |
| `chronology/commlink_index.json` | The raw comm-link archive index |
| `chronology/title_patch.json` | Titles recovered for videos missing from the original metadata |

Transcripts are machine-produced (auto-captions or Whisper) and will contain recognition errors, especially in names
and ship designations. When it matters, check the video.

## How it is used

[SC Toolbox](https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2) streams this repository rather than bundling it:
the tool downloads the small index once and fetches an individual transcript only when you open a result, from
`raw.githubusercontent.com`.

To watch the source of any transcript: `https://www.youtube.com/watch?v=<video_id>` (the file name is the id).
