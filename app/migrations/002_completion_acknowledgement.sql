-- Knowing a job finished while nobody was looking.
--
-- The product is built for work that runs for hours while you do something
-- else, and until now it could not tell you it was done. The specification
-- excludes OS notification registration, launchd, systemd, and any outbound
-- call, so there is no push channel available and none is wanted: the whole
-- pitch is that nothing leaves this computer.
--
-- What is left is telling you when you come back. That needs one durable fact
-- — whether you have already been told — because the alternative is a banner
-- that reappears on every page load forever, or one held in browser storage
-- that forgets the moment you open a different tab.
--
-- Nullable rather than defaulted: NULL means "finished, not yet seen", which is
-- exactly the state that existed for every job before this column did.

ALTER TABLE jobs ADD COLUMN completion_acknowledged_at TEXT;

CREATE INDEX idx_jobs_unacknowledged
    ON jobs (completion_acknowledged_at, status);
