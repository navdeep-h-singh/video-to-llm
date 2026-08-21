# Running it day to day

## The two processes

The **interface** shows and controls jobs. The **worker** owns them.

That split is the reason closing the browser is harmless. `video-to-llm start`
runs both; `start-ui` and `run-worker` run them separately, which is useful when
you want the worker on a machine you leave running and the interface open only
sometimes.

Exactly one worker may own an output folder. A second is refused, with a message
saying so. The guard is an OS-level file lock plus a database claim with a
heartbeat — the lock is released by the kernel however the process dies, and the
claim catches network filesystems where advisory locking is unreliable.

## Commands

| Command | What it does |
|---|---|
| `start` | Interface and worker together |
| `start-ui` | Interface only |
| `run-worker` | Worker only. `--once` processes what is waiting, then exits |
| `doctor` | Is this computer ready, and what is missing |
| `status` | Jobs and worker health, without opening a browser |
| `smoke-test` | Twelve end-to-end checks on generated media, no network |
| `import <path>` | Bring previously processed output under management |

## While a job runs

**Pause** stops after the current step. Everything finished is kept, and
resuming picks up where it stopped rather than starting over.

How soon it takes effect depends on what the job is doing. Between videos, and
between stages, the worker checks before it starts the next one. Inside the
description stage — the long one, and the only one that can cost money — it
checks before every batch, so on a paid provider nothing further is sent once
you have asked it to stop. Taking pictures and writing the transcript are single
FFmpeg and Whisper calls that run to the end of the current video before the
pause lands.

**Cancel** stops for good — and still keeps everything already produced.
Cancelling is not undoing. Frames, transcripts, and descriptions on disk cost
time and possibly money; you asked to stop, not to throw them away.

Both are safe at any moment. Nothing is left half-written, because every artifact
is written to a temporary sibling and atomically renamed into place.

## Reading the states

| State | Means |
|---|---|
| Draft | Made, not started |
| Ready to start | Waiting for the worker |
| Preparing | Taking pictures |
| Writing the transcript | Turning speech into text |
| Describing pictures | Stage 3, if switched on |
| Waiting to try again | A temporary problem; backing off |
| Paused | You stopped it |
| Needs you | Something could not be finished |
| Finished | Everything completed |
| Finished, with gaps | Everything ran, some pictures have no description |
| Cancelled | You stopped it for good |

**Finished, with gaps** is deliberately distinct from both neighbours. Collapsing
it into *Finished* would hide a real shortfall behind a green tick; calling it
*Needs you* would imply something is broken when the job did exactly what it
could.

Every state shows a word, a shape, and a colour. Colour is never the only signal.

## Where things go

```
<output root>/
  state.db                     jobs, stages, batches, artifacts, events
  worker.lock                  which process owns this folder
  <job-id>/
    <video-id>_v<n>/
      frames/                  clean 1280x720 pictures
      frames_api/              numbered copies, only if descriptions are on
      frames_manifest.json     index, time, filenames, batches
      transcript.json          words with timings, silences marked
      silence_windows.json     where the quiet stretches are
      visual_results.json      descriptions, if any
      gaps.txt                 pictures without a description, if any
      assembled.txt            everything, in time order
    master_assembled.txt       multi-video jobs only
    provenance.json            what made this, with which settings
    analysis_input/            ready to hand on
  collections/<id>/v<n>/       collection output, kept separate
```

## Costs

A local-only job shows **No provider API charge** — not "$0.00", because the run
still costs battery, heat, memory, and time.

An external provider shows a running total against your cap. The cap is checked
*before* each batch is sent, so it is a limit rather than a report.
