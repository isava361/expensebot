CREATE TABLE IF NOT EXISTS users(
    tg_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS "groups"(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    owner_tg_id INTEGER NOT NULL,
    invite_code TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS group_members(
    group_id INTEGER NOT NULL,
    tg_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    PRIMARY KEY(group_id, tg_id),
    FOREIGN KEY(group_id) REFERENCES "groups"(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS expenses(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    payer_tg_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(group_id) REFERENCES "groups"(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS expense_participants(
    expense_id INTEGER NOT NULL,
    participant_tg_id INTEGER NOT NULL,
    share_cents INTEGER NOT NULL,
    PRIMARY KEY(expense_id, participant_tg_id),
    FOREIGN KEY(expense_id) REFERENCES expenses(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS settlements(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER,
    from_tg_id INTEGER NOT NULL,
    to_tg_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    confirmed_by_to INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
