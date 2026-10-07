# signal-copier

Следи Telegram канал за трейдинг сигнали и ги копира в OANDA (v20 REST).
Режими: `record` (по подразбиране, без търговия) → `paper` → `live`.

> Статус: стъпка 1–2 готови (структура, модели, парсер + тестове).
> Останалите модули предстоят. Инструкции за Telethon вход и деплой ще бъдат добавени в стъпка 3.

## Структура

```
signal-copier/
├── pyproject.toml
├── config.example.yaml        # всички настройки (копира се в config.yaml)
├── .env.example               # тайни (копира се в .env, никога в git)
├── src/signal_copier/
│   ├── models.py              # Signal, Entry, FollowUp, IncomingMessage, ParseResult
│   ├── settings.py            # (стъпка 3) зареждане на config.yaml + .env, проверка LIVE_CONFIRM
│   ├── parser/                # ✅ текст -> Signal / FollowUp
│   │   ├── rules.yaml         #    regex правила + symbol mapping (настройваемо)
│   │   ├── rules.py
│   │   ├── symbols.py
│   │   └── parser.py
│   ├── listener/              # (стъпка 3) Telethon: нови + редактирани съобщения, без повторение след рестарт
│   ├── risk/                  # (стъпка 3) размер на позиция, лимити, спред, slippage, възраст, дедупликация
│   ├── executor/              # (стъпка 3) OANDA адаптер, разделяне по TP, follow-ups, idempotent retry
│   ├── storage/               # (стъпка 3) SQLite: messages, signals, orders, trades, events
│   ├── notifier/              # (стъпка 3) собствен бот: известия + /stop /status
│   ├── backtest/              # (по-късно) оценка на record данните с M1 свещи, спред и забавяне
│   └── app.py                 # (стъпка 3) сглобява всичко според MODE
├── tests/
│   ├── test_parser.py
│   └── test_symbols.py
└── deploy/                    # (стъпка 3) Dockerfile, docker-compose, systemd unit
```

## Модели (src/signal_copier/models.py)

- `Signal`: `symbol` (OANDA, напр. `XAU_USD`), `raw_symbol`, `side` (BUY/SELL),
  `entry` (`MARKET` / `LIMIT` / `STOP` с цена или зона), `sl` (може да е `None` → risk го отхвърля),
  `tp[]`, `raw_text`, `chat_id`, `message_id`, `index` (при няколко сигнала в едно съобщение),
  `edited`, `warnings`. Ключ за дедупликация: `chat_id:message_id:index`.
- `FollowUp`: `MOVE_SL_BE`, `MOVE_SL` (към цена), `CLOSE`, `CLOSE_PARTIAL` (`fraction`),
  `TP_HIT`, `SL_HIT`, `CANCEL`. Целта се намира по `reply_to_msg_id`, иначе по `symbol`;
  без нито едно от двете → не се гадае, праща се известие.
- `ParseResult.status`: `SIGNAL` / `FOLLOW_UP` / `UNCERTAIN` / `IGNORED`.
  При `UNCERTAIN` нищо от съобщението не се търгува – записва се и се праща известие.

## Парсер

Нормализира текста (емоджита, `XAU/USD`, `2,345.50`, тирета), разделя съобщението на
блокове по редовете със символ и за всеки блок извлича посока, entry (цена, зона, `2350-53`),
SL и TP. Проверки за увереност: SL/TP от правилната страна на entry, цени в разумен диапазон
(`max_price_deviation_pct`), без противоречиви посоки. Всички regex-и и символи са в `parser/rules.yaml`.

## Тестове

```bash
pip install -e ".[dev]"
pytest
```
