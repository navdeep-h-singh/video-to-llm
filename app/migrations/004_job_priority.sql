-- Which job the worker should run next.
--
-- The worker takes the single oldest `ready` job and does not come back until
-- every video in it is finished, so a job queued behind a long one waits for
-- the whole thing. Observed 2026-08-12: a thirteen-video job sat at `ready`
-- with `started_at` NULL while a one-video job ahead of it ground through local
-- descriptions for hours, and only started when that job left the loop.
--
-- Higher runs sooner; ties fall back to `created_at`, so the default of 0
-- leaves the existing first-in-first-out order exactly as it was. Nothing
-- reorders itself: a number here only ever changes because someone asked for
-- it, which is what keeps two jobs from taking turns pushing each other aside.
--
-- Deliberately not a queue position. Positions have to be renumbered whenever
-- anything is inserted or removed, and every renumbering is a chance to write
-- the wrong order into a row that nobody was touching. A monotonic priority is
-- append-only: "run this next" reads the current maximum and adds one.

ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;

-- The worker's claim query orders by priority then age, and runs on every turn
-- of the loop.
CREATE INDEX idx_jobs_queue ON jobs (status, priority DESC, created_at);
