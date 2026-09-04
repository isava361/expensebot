-- Leaving a group is now possible, and when the owner leaves the group has
-- to keep an owner: it passes to whoever joined earliest. Membership rows
-- never recorded when they were created, so add it. Rows that predate this
-- keep 0 and are therefore treated as the oldest, which is true of them.
ALTER TABLE group_members ADD COLUMN joined_at INTEGER NOT NULL DEFAULT 0;
