# What each stage promises

Six stages. Each writes its artifacts atomically, registers them only once they
are durably on disk, and is skipped entirely if it has already completed — so
restarting a job resumes rather than redoing.

---

## Stage 1 — Pictures

**In:** a video file, referenced by absolute path. Never copied, never moved,
never modified.

**Out:** `frames/`, optionally `frames_api/`, and `frames_manifest.json`.

Sampling is a **fixed interval only**. There is no scene detection, deliberately:
a fixed grid means picture *n* is always at a predictable time, which is what
makes the frame map, reruns, and collection references reproducible years later.

Timestamps are computed as `index × interval`, not accumulated. Over a two-hour
video at half-second sampling that is 14,400 additions of a float, and the drift
would put late frames off the grid.

Two sets of pictures, kept apart on purpose:

- **`frames/`** — clean 1280×720 JPEGs, exactly as captured. What you review and
  what ends up in an export.
- **`frames_api/`** — smaller copies with `IDX nn` stamped in the top-left. The
  *only* images ever sent to a description model. Written only when descriptions
  are switched on.

The stamp exists because a model given twenty unlabelled pictures will sometimes
answer about them out of order. Without it, that misalignment is undetectable.

`frame_interval_ms` becomes **immutable** once extraction begins. A different
interval means a deliberate new version, never a mutation of this one.

---

## Stage 2 — Transcript

**In:** the same video.

**Out:** `transcript.json`, `transcript.txt`, `silence_windows.json`.

Audio is extracted to 16 kHz mono. Stretches of quiet longer than three seconds
are detected and recorded — silence is evidence, not absence.

Each non-silent stretch is transcribed separately with a little padding, and its
timestamps are **remapped back onto the original video timeline**. This is the
part that matters most: without the remap, every time after the first gap would
be early, and a transcript with plausible-but-wrong times is worse than none.

The backend resolver tries `auto`/`cpu`/`metal`/`cuda`/`vulkan` and **always
falls back to the processor**. Metal and Vulkan report plainly that no
accelerated path exists rather than letting you believe one is in use. The
fallback and its reason are recorded in provenance.

A video with no audio completes normally, with no transcript. That is not a
failure.

---

## Stage 3 — Descriptions *(optional, off by default)*

**In:** the numbered copies from `frames_api/`. Never the video, never the audio,
never a filename or path.

**Out:** `visual_results.json`, per-batch artifacts, and `gaps.txt` if anything
was skipped.

Every returned description must claim an index that was actually sent. Invented,
missing, and duplicated indexes are all rejected — accepting a partial answer
positionally would attach every later description to the wrong moment.

`Unknown` is preserved, never replaced with a guess. Unparseable confidence maps
to Low, because promoting a guess to High turns it into evidence.

Batches: up to 20 for a cloud service; 1 by default for a local model, 2 after a
successful preflight, 4 only with an advanced override. The budget is checked
*before* each send.

A batch that cannot be described becomes a visible gap. The job finishes as
**Finished, with gaps** rather than failing.

---

## Stage 4 — Enrichment

Rules, not a model. No network, no provider, and the same input always gives the
same output — so two runs of a video can be compared.

Derives emphasis (low confidence, unreadable frames, explicit actions, long
silences), timeframe and instrument switches, and time-window segments.

An `Unknown` between two identical readings is **not** a switch: the model could
not see the value in that frame, not that you changed instrument and changed
back. Segments shorter than 30 seconds fold into their neighbour, or a recording
that flicks between charts would produce hundreds of one-line headings.

---

## Stage 5 — Assembly

**Out:** `assembled.txt` per video; `master_assembled.txt` per job, **only** when
the job holds more than one video.

Organised by **time**, not by source. A transcript line and a frame description
from the same second belong next to each other; grouping all the transcript
first would preserve every fact and destroy what makes them useful together.

Unreadable fields are omitted with a count rather than printed as a column of
`Unknown`, so the reader knows the frame was seen and mostly unreadable rather
than skipped.

Multi-video order is the order you confirmed. Never inferred from filename or
date — two recordings from the same morning have no inherent order, and guessing
wrong silently reverses the narrative.

---

## Stage 6 — Handoff

**Out:** `analysis_input/` with the assembled documents, references to the clean
pictures, a README explaining how `picture 47` maps to a file, and a manifest.
Plus `provenance.json` for the job.

Pictures are symlinked rather than copied — a 1,265-frame video is about 1.7 GB
and copying would double the job's disk cost. Where symlinks are unavailable it
falls back to copying, so the handoff always works. A portable export copies
deliberately.

Provenance records the source **filename** and its checksum, never the absolute
path. The layout of your disk is not part of the evidence.
