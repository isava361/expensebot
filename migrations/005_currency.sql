-- Amounts used to be unitless, so a trip that crossed a border added up
-- lira and roubles into one meaningless number.
--
-- Every group now has one base currency: expenses.amount_cents, settlements
-- and all balances are in it, so the existing arithmetic keeps working
-- untouched. An expense actually paid in another currency also keeps what
-- was handed over (orig_amount_cents in orig_currency), which is what a
-- person sees on their receipt and bank statement.
--
-- Groups that predate this are stamped RUB rather than DEFAULT_CURRENCY:
-- their numbers were entered under some currency already, and the bot's
-- users are Russian-speaking, so RUB is the honest guess. A group with no
-- expenses yet can be switched in its settings.
ALTER TABLE "groups" ADD COLUMN currency TEXT NOT NULL DEFAULT 'RUB';
ALTER TABLE expenses ADD COLUMN orig_currency TEXT;
ALTER TABLE expenses ADD COLUMN orig_amount_cents INTEGER;
