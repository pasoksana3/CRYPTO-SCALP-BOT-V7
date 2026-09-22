# V7.1 FIXED

Робочий порядок: 1H → 15m → 10m → 5m.

Виправлено:
- прибрано блокуючу вимогу, щоб останні 1H свічки обов'язково торкались HTF-зони;
- вибирається актуальна/найближча HTF imbalance або order block;
- перевіряються LONG і SHORT, а не лише один напрям;
- рішення приймаються по закритих свічках;
- у Railway логуються всі етапи: zone → liquidity → CHoCH/BOS → 10m → 5m → RR;
- додано обробку помилок Telegram;
- додано невелику паузу між монетами для зменшення burst-запитів MEXC;
- cooldown зберігається під час роботи процесу.

Railway:
1. Завантажити файли.
2. Встановити змінні TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID.
3. За потреби змінити SYMBOLS/POLL_SECONDS/LEVERAGE.
4. Start command: python main.py.
