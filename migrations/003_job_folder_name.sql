-- Where a job's files live, by name rather than by identifier.
--
-- Output folders were named with the job's UUID, so the output root was a list
-- of thirty-two-character hex strings and finding a particular job's files
-- outside this application meant reading the database. The name the user typed
-- is the only handle they have on the job, and it should be the handle on disk
-- too.
--
-- Stored rather than derived. Deriving the folder from the current name would
-- move where the worker writes the moment a job is renamed, orphaning
-- everything produced up to that point — and a rename is cheap precisely
-- because it is only a label. This column is written once, when the job is
-- created, and never changes afterwards.
--
-- NULL means "named with the identifier", which is every job that existed
-- before this migration. Those keep working exactly as they did: the fallback
-- is the job id, so no file has to move and no path already recorded in
-- job_videos.output_dir becomes wrong.

ALTER TABLE jobs ADD COLUMN output_dirname TEXT;
