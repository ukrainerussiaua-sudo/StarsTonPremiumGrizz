# 🐻 Grizz Shop Bot v2

Telegram-бот для продажу Stars, Premium та крипти (TON/GRAM).

---

## Налаштування

### 1. Заповніть `env` файл

```
BOT_TOKEN=         # токен від @BotFather
ADMIN_IDS=         # ваш Telegram ID (можна кілька через кому: 111,222)
CRYPTO_API_TOKEN=  # токен від @CryptoBot → /pay → Create App
SUPABASE_URL=      # з Supabase Project Settings → API
SUPABASE_KEY=      # service_role key (НЕ anon!)
UAH_PER_STAR=4.5
UAH_PER_MONTH_PREMIUM=280
UAH_PER_TON=200
UAH_PER_GRAM=140
```

### 2. Виконайте SQL міграції

В Supabase → SQL Editor виконайте файл `supabase_schema_v2.sql`.

### 3. Встановіть залежності

```bash
pip install -r requirements.txt
```

### 4. Запустіть

```bash
python bot.py
```

---

## Що нового у v2

### Поповнення балансу (новий флоу)
- Якщо балансу не вистачає — одразу показує скільки не вистачає і кнопку «Поповнити баланс»
- **💳 UAH картка** — вибір банку → PDF квитанція → автоперевірка на check.gov.ua → моментальне зарахування
- **🤖 CryptoBot** — інвойс в USDT → захист від подвійного зарахування через БД
- **◎ TON** — автоматична перевірка транзакції через toncenter API по коментарю (Telegram ID)

### 30-хвилинний дедлайн
- Кожне замовлення має дедлайн +30хв
- Фоновий воркер кожні 5 хвилин перевіряє прострочені замовлення
- Адмін отримує нагадування з кнопкою «Позначити виконаним»

### Захист від дублів
- Квитанція UAH: зберігається в `used_receipts`, повторно не приймається
- CryptoBot: invoice_id зберігається в `pending_crypto_topups`, статус `credited` блокує повторне нарахування
- TON: comment (= user_id) в `pending_ton_topups`, після зарахування статус `credited`

### Адмін-панель
- Перегляд очікуючих відправок з позначкою ⚠️ якщо прострочено
- Підтвердження/скасування відправок криптовалюти
- Відповідь на тікети підтримки прямо з панелі
- Зміна цін, реквізитів, TON/GRAM гаманців

---

## Структура БД

| Таблиця | Призначення |
|---------|-------------|
| `users` | Юзери, баланс, витрати |
| `orders` | Всі замовлення + deadline |
| `settings` | Ціни, реквізити, адреси |
| `used_receipts` | Використані квитанції |
| `pending_sends` | Відправки TON/GRAM на підтвердження адміном |
| `pending_crypto_topups` | Pending CryptoBot-поповнення |
| `pending_ton_topups` | Pending TON-поповнення |
