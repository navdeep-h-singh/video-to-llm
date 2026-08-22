---
name: video-to-llm
description: >-
  Read a local video file — lecture, course, recording, screencast — by turning
  it into one timestamped document, once, and reusing it for every question
  afterwards. Use when the user points at a video file or folder of videos and
  wants its contents understood, searched, summarised, quoted, or checked. Also
  use to resolve a timestamp back to the exact frame behind a claim. Runs
  entirely on the user's machine; nothing is uploaded. Built for long video —
  hours, not minutes.
---

# Reading a video

You cannot watch a video. This skill turns one into text you can read, and it
does the expensive part **once**: after a video is processed, every later
question about it is instant and free.

That is the important difference from other approaches. Do not re-process a
video to answer a follow-up question. Check what is already processed first.

## Before anything else

The tool must be installed and FFmpeg must be on the PATH.

```bash
video-to-llm doctor
```

If the command is missing:

```bash
uv tool install video-to-llm    # or: pipx install video-to-llm
```

## The loop

### 1. See what is already there

```bash
video-to-llm status
```

If the video the user is asking about has already been processed, skip straight
to step 3. Processing a forty-minute video takes minutes; reading one that is
already done takes no time at all.

### 2. Process it, once

```bash
video-to-llm process "/path/to/lecture.mp4"
```

It prints the path of the assembled document when it finishes. Useful flags:

| Flag | Use it when |
|---|---|
| `--interval 5` | Long video, and the user cares about speech more than the screen. Fewer pictures, faster. |
| `--interval 1` | Dense screen content — code, slides, charts changing quickly. |
| `--name "Week 3"` | The filename is unhelpful. This becomes the folder name and how you refer to it later. |
| `--describe local` | The user needs to know what was *on screen*, not just what was said. **Slow — roughly 30 seconds per picture.** Say so before starting, and prefer a larger `--interval`. |
| `--format jsonl` | You want to process the timeline programmatically rather than read it. |

Several videos in one job, in the order given:

```bash
video-to-llm process week1.mp4 week2.mp4 week3.mp4 --name "The course"
```

**Tell the user before you start on anything long.** A two-hour video is not a
two-second command. Give them the chance to pick a coarser interval.

### 3. Read the document

```bash
cat "/path/printed/by/process/assembled.txt"
```

It is chronological: speech, marked silences, section headings, and — if
descriptions were on — what was on screen, all interleaved in the order it
happened, every line carrying a timestamp.

For a long video, read the range you need rather than the whole file.

### 4. Cite what you claim

Every line has a time. When you tell the user something the video said, give
them the timestamp, and when they want to check it:

```bash
video-to-llm show "The course" 00:12:34
```

That prints the surrounding transcript and the **path to the exact frame**. Hand
them that path. A claim they can check is worth more than one they cannot.

## Rules

- **Never re-process a video that is already done.** Run `status` first. This is
  the single most common mistake and it costs the user real minutes.
- **Say what a long run will cost in time before starting it**, especially with
  `--describe local`.
- **Do not turn on cloud descriptions.** `--describe` accepts a paid service,
  but that is the user's decision to make in the interface where the estimate
  and the spending cap are visible. If they want it, point them there.
- **Quote timestamps, not impressions.** The document's value is that it can be
  checked.
- **Local video files only.** This tool does not download from URLs. If the user
  gives you a link, ask them to download it first.

## What it does not do

It is not a video editor, a summariser, or a reasoning engine. It produces
evidence; the reading is yours.

Transcription accuracy is unmeasured, and Whisper sometimes hallucinates a line
over silence — a transcript that opens with "Thanks for watching!" over a quiet
introduction is the model, not the video. Treat a single odd line as suspect and
check it with `show` before repeating it.
