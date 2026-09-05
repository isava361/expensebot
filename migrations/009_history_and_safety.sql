ALTER TABLE expenses ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE expenses ADD COLUMN operation_id TEXT;
CREATE UNIQUE INDEX expenses_operation ON expenses(operation_id);

CREATE TABLE expense_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_id INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
    actor_tg_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    before_json TEXT,
    after_json TEXT NOT NULL
);
CREATE INDEX expense_history_expense ON expense_history(expense_id, id);
CREATE INDEX expenses_group_active ON expenses(group_id, deleted, id);
CREATE INDEX memberships_user ON group_members(tg_id, group_id);
CREATE INDEX settlements_group_confirmed ON settlements(group_id, confirmed_by_to);
CREATE INDEX settlements_batch ON settlements(batch);

-- Preserve the initiator even for a zero-net offset.
ALTER TABLE settlements ADD COLUMN requested_by INTEGER;
ALTER TABLE settlements ADD COLUMN requested_to INTEGER;

CREATE TRIGGER expense_amount_insert BEFORE INSERT ON expenses
WHEN typeof(NEW.amount_cents) != 'integer' OR NEW.amount_cents <= 0 OR NEW.amount_cents > 1000000000
BEGIN
    SELECT RAISE(ABORT, 'invalid expense amount');
END;
CREATE TRIGGER expense_amount_update BEFORE UPDATE OF amount_cents ON expenses
WHEN typeof(NEW.amount_cents) != 'integer' OR NEW.amount_cents <= 0 OR NEW.amount_cents > 1000000000
BEGIN
    SELECT RAISE(ABORT, 'invalid expense amount');
END;
CREATE TRIGGER share_amount_insert BEFORE INSERT ON expense_participants
WHEN typeof(NEW.share_cents) != 'integer' OR NEW.share_cents < 0 OR NEW.share_cents > 1000000000
BEGIN
    SELECT RAISE(ABORT, 'invalid share amount');
END;
CREATE TRIGGER share_amount_update BEFORE UPDATE OF share_cents ON expense_participants
WHEN typeof(NEW.share_cents) != 'integer' OR NEW.share_cents < 0 OR NEW.share_cents > 1000000000
BEGIN
    SELECT RAISE(ABORT, 'invalid share amount');
END;
