-- ═══════════════════════════════════════════════════════════════
-- Grizz Shop v2 — SQL міграції
-- Виконати в Supabase SQL Editor
-- ═══════════════════════════════════════════════════════════════

-- 1. Додаємо поле deadline до існуючої таблиці orders
ALTER TABLE orders ADD COLUMN IF NOT EXISTS deadline TIMESTAMP WITH TIME ZONE;

-- 2. Таблиця для відстеження pending CryptoBot-поповнень (захист від дублів)
CREATE TABLE IF NOT EXISTS pending_crypto_topups (
    id            SERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL,
    invoice_id    BIGINT NOT NULL UNIQUE,
    amount_usdt   REAL NOT NULL,
    amount_uah    REAL NOT NULL,
    status        TEXT DEFAULT 'pending',  -- pending | credited | expired
    created_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    credited_at   TIMESTAMP WITH TIME ZONE
);

-- 3. Таблиця для відстеження pending TON-поповнень (захист від дублів)
CREATE TABLE IF NOT EXISTS pending_ton_topups (
    id            SERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL,
    comment       TEXT NOT NULL,           -- = Telegram user_id як рядок
    amount_ton    REAL NOT NULL,
    amount_uah    REAL NOT NULL,
    status        TEXT DEFAULT 'pending',  -- pending | credited | expired
    created_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    credited_at   TIMESTAMP WITH TIME ZONE,
    UNIQUE(user_id, status)               -- один активний pending на юзера
);

-- 4. Індекси для швидкого пошуку
CREATE INDEX IF NOT EXISTS idx_pending_crypto_invoice ON pending_crypto_topups(invoice_id);
CREATE INDEX IF NOT EXISTS idx_pending_crypto_status  ON pending_crypto_topups(status);
CREATE INDEX IF NOT EXISTS idx_pending_ton_comment    ON pending_ton_topups(comment);
CREATE INDEX IF NOT EXISTS idx_pending_ton_status     ON pending_ton_topups(status);
CREATE INDEX IF NOT EXISTS idx_orders_status          ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created         ON orders(created_at);

-- ═══════════════════════════════════════════════════════════════
-- Для перевірки: поточні таблиці
-- SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';
-- ═══════════════════════════════════════════════════════════════
