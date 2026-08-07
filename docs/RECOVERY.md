# When things go wrong

The short version: **valid work survives, and nothing expensive is repeated.**

## What is guaranteed

**A file that exists is complete.** Every artifact is written to a temporary
sibling, flushed, fsynced, atomically renamed into place, and the directory
fsynced after. A process killed mid-write leaves the previous good version
untouched and an obviously-temporary file behind, which the next start removes.
This is tested by actually `SIGKILL`ing a process between the write and the
rename.

**A completed batch is never sent again.** A batch is marked complete only after
its artifact is durably on disk. With an external provider, this is the
difference between resuming a job and paying for it twice.

**Pause is respected by recovery.** Restarting does not resurrect a job you
deliberately stopped.

## What happens on start-up

1. Files left over from an interrupted write are removed. They are unreferenced
   by construction — one can only exist because a process died between creating
   it and renaming it.
2. Rows pointing at files that are no longer on disk are de-registered. A row
   pointing at nothing would mislead every later stage into believing the work
   exists.
3. In-flight batches go back to pending. Completed ones are untouched.
4. Interrupted jobs return to **Ready to start**, not *Needs you* — an
   interruption is the ordinary consequence of closing a laptop.
5. What was recovered is written into the job's event log in plain language.

Reconciliation is safe to run repeatedly and reports honestly when there was
nothing to repair.

## Specific situations

**The laptop slept mid-job.** Nothing to do. The worker resumes; the event log
records the interruption.

**The worker was killed.** Start it again. The lock is released by the operating
system whatever happened to the process.

**"Another worker already owns this output folder."** One is already running.
Stop it first, or point this one at a different folder. If the other process is
genuinely gone, its claim ages out after two minutes and the next start takes
over — the window is wide enough that a briefly suspended laptop is not mistaken
for a dead worker.

**The external drive was unplugged.** Reconnect it and start the worker. Missing
artifacts are de-registered and can be produced again; nothing crashes.

**A video failed but the others were fine.** The job reports *Needs you* and
names the video. The rest completed normally and their output is intact.

**Some pictures have no description.** The job is *Finished, with gaps*, and
`gaps.txt` lists which and why. The frames, transcript, and assembled document
are all still there. You can ask for just those pictures again later without
redoing anything else.

**The database will not open.** It is reported, never silently replaced —
overwriting it would destroy the record of what has already been paid for. The
artifacts on disk are independent of it and are still readable; `import` can
bring them back under management.

## What is not automatic

Validated work is never re-run on its own. A rerun is always an explicit,
versioned request, and it never overwrites the previous version's output.
