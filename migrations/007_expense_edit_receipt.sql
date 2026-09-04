-- A typo in an amount used to be unfixable: the only lever was deleting the
-- expense and entering it again, which loses its number and its place in the
-- history. Expenses can now be edited by the person who created them, and
-- updated_at records that it happened, so a screen can say so out loud.
ALTER TABLE expenses ADD COLUMN updated_at INTEGER;

-- A photo of the receipt is the thing people actually re-check a split
-- against, so keep Telegram's file id next to the expense.
ALTER TABLE expenses ADD COLUMN receipt_file_id TEXT;
