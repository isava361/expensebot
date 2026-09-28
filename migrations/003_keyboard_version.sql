-- Existing users keep a stale reply keyboard until the bot sends them a new
-- one, so track which layout each user has actually been shown. Rows that
-- predate this migration default to 0 and get refreshed on their next message;
-- users created afterwards are stamped with the current version on insert.
ALTER TABLE users ADD COLUMN keyboard_version INTEGER NOT NULL DEFAULT 0;
