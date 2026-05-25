ALTER TABLE expenses ADD COLUMN created_by_tg_id INTEGER;

UPDATE expenses
SET created_by_tg_id = payer_tg_id
WHERE created_by_tg_id IS NULL;
