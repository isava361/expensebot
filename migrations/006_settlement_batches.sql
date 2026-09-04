-- A payment used to be recorded by the payer alone: the debtor pressed
-- "I paid" and the debt vanished, with the creditor merely notified. The
-- confirmed_by_to column existed but was always written as 1.
--
-- Settling with someone closes their debt in every shared group at once, so
-- one payment is several rows. They now share a batch id, which is what the
-- creditor confirms or rejects — all of it, or none of it.
ALTER TABLE settlements ADD COLUMN batch TEXT;

-- Rows that predate this were already treated as confirmed; give each its
-- own batch so nothing can confirm or reject them as a group later.
UPDATE settlements SET batch = 'legacy-' || id WHERE batch IS NULL;
