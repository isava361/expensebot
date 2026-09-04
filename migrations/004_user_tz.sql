-- Timestamps used to be rendered in the server's local time for everyone.
-- Store each person's own offset instead; 0 (UTC) is the honest default for
-- rows that predate this, and new users get DEFAULT_TZ_OFFSET.
ALTER TABLE users ADD COLUMN tz_offset_min INTEGER NOT NULL DEFAULT 0;
