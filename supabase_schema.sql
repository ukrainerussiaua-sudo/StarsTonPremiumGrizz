-- ── Користувачі ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id                SERIAL PRIMARY KEY,
    user_id           BIGINT UNIQUE NOT NULL,
    username          TEXT DEFAULT '',
    first_name        TEXT DEFAULT '',
    balance_uah       REAL DEFAULT 0.0,
    total_bought_uah  REAL DEFAULT 0.0,
    created_at        TIMESTAMP DEFAULT NOW()
);

-- ── Замовлення ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    product     TEXT NOT NULL,
    amount_uah  REAL NOT NULL,
    payload     TEXT DEFAULT '',
    status      TEXT DEFAULT 'pending',
    created_at  TIMESTAMP DEFAULT NOW()
);

-- ── Налаштування (key-value) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS settings (
    id    SERIAL PRIMARY KEY,
    key   TEXT UNIQUE NOT NULL,
    value TEXT DEFAULT ''
);

-- ── Використані квитанції (захист від повторного використання) ───────────
CREATE TABLE IF NOT EXISTS used_receipts (
    id            SERIAL PRIMARY KEY,
    receipt_code  TEXT UNIQUE NOT NULL,
    bank          TEXT NOT NULL,
    amount        REAL NOT NULL,
    user_id       BIGINT NOT NULL,
    created_at    TIMESTAMP DEFAULT NOW()
);

-- ── Очікуючі відправки TON/GRAM ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pending_sends (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    coin        TEXT NOT NULL,
    to_address  TEXT NOT NULL,
    amount      REAL NOT NULL,
    amount_uah  REAL NOT NULL,
    status      TEXT DEFAULT 'pending',
    created_at  TIMESTAMP DEFAULT NOW()
);

-- ── Початкові налаштування ────────────────────────────────────────────────
INSERT INTO settings (key, value) VALUES
    ('uah_per_star',           '4.5'),
    ('uah_per_month_premium',  '280'),
    ('ton_to_uah',             '200'),
    ('gram_to_uah',            '140'),
    ('usdt_to_uah',            '40'),
    ('ua_card_number',         '4441 1111 2222 3333'),
    ('ua_card_holder',         'MYKYTA P'),
    ('ua_card_initials',       'Микита П.'),
    ('admin_ton_address',      'UQAxxx...'),
    ('admin_gram_address',     'EQAxxx...')
ON CONFLICT (key) DO NOTHING;
