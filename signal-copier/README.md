# signal-copier

Следи Telegram канал за трейдинг сигнали и ги копира в OANDA (v20 REST API).

| Режим | Какво прави |
|---|---|
| `record` (по подразбиране) | Само чете, парсва и записва сигналите и командите в SQLite. Никаква търговия. |
| `paper` | Търгува на OANDA **practice** акаунт. |
| `live` | Търгува на реален акаунт. Стартира само ако в `.env` има `LIVE_CONFIRM=yes`. |

Препоръчителен ред: седмици в `record` → оценка дали групата е печеливша (скрипт с M1 свещи, предстои) → `paper` → чак тогава `live`.

## Как работи

```
Telegram канал ──► listener/ ──► parser/ ──► risk/ ──► executor/ ──► OANDA
   (Telethon)        нови +       Signal /     размер,     market/limit,
                     редакции     FollowUp     лимити      SL/TP, retry
                         │            │           │            │
                         └────────────┴─── storage/ (SQLite) ──┘
                                              │
                                     notifier/ ──► твоят Telegram бот (/stop /status)
```

- **Сигнал** (`AUDUSD / Buy now @ … / Target Profit 1 @ … / Stop Loss @ …`): ако минат всички risk проверки, се отваря по една сделка за всеки TP (обемът се дели поравно), всяка с прикачени SL и TP.
- **Команди** (reply към сигнала или със символ): `move SL to BE`, `move SL to 1.2345`, `close`, `close half` / `close 30%`, `cancel`, `TP1 hit`, `SL hit`.
- **Несигурно съобщение** → не се търгува, записва се, получаваш известие с текста.
- **Редактиран сигнал**: ако е изпълнен и SL/TP са сменени → местят се SL/TP на отворените сделки. Ако е бил отхвърлен (напр. без SL) и редакцията го оправя → оценява се отново. Смяна на символ/посока → само известие.
- **Редактирана команда** (напр. каналът редактира „Close…“) → не се изпълнява втори път.

### Risk проверки (`config.yaml → risk`)

Размер по фиксиран % риск от баланса спрямо разстоянието до SL (с конверсия към валутата на акаунта); отхвърля без SL; max отворени позиции и max на символ; дневен лимит на загуба (% от NAV в началото на деня, UTC) → спира нови сделки до утре; отхвърля, ако цената е отишла по-далеч от `max_entry_slippage_sl_fraction` × SL разстоянието, ако цената вече е зад SL или TP1, ако сигналът е по-стар от `max_signal_age_seconds`; проверка на спреда (`max_spread` по символ или % от SL); дедупликация по message_id.

### Без двойни поръчки

Всяка част от поръчката има детерминистично client ID (`sc_<chat>_<msg>_<idx>_<part>`), което се записва в SQLite **преди** изпращане и се праща към OANDA. При мрежова грешка ботът първо пита OANDA за това ID и праща отново само ако OANDA никога не го е получавала. При рестарт: Telethon не преизпълнява пропуснати съобщения, всяко съобщение се дедупликира, а редакции на стари съобщения, които ботът не е виждал, се игнорират.

## Първоначална настройка

Нужно ти е: VPS с Docker (или Python 3.11+), Telegram акаунт, който е член на канала, OANDA practice акаунт.

1. **Telegram API ключ** (за userbot-а, чете канала като теб): влез в <https://my.telegram.org> → *API development tools* → създай приложение → вземи `api_id` и `api_hash`.
2. **Твой бот за известия**: в Telegram пиши на `@BotFather` → `/newbot` → вземи токена. Пиши каквото и да е на новия бот, после отвори `https://api.telegram.org/bot<TOKEN>/getUpdates` и вземи `message.chat.id` – това е `NOTIFY_CHAT_ID`. Ботът изпълнява команди само от този chat.
3. **OANDA**: в practice акаунта → *Manage API Access* → генерирай токен; account ID е във вида `101-004-1234567-001`.
4. Конфигурация:
   ```bash
   git clone https://github.com/dimitrov06/dimitrov06.git && cd dimitrov06/signal-copier
   cp .env.example .env            # попълни тайните; .env никога не влиза в git
   cp config.example.yaml config.yaml
   # в config.yaml: telegram.channels (напр. "@rted_premium" или числово id -100…), risk, mode
   mkdir -p data && sudo chown 1000:1000 data   # контейнерът пише тук като uid 1000
   ```
   Ако каналът е частен и няма @username, използвай числовото му id (`-100…`). Можеш да го видиш в Telegram Desktop: Settings → Advanced → Experimental settings → Show peer IDs.

### Първоначален Telethon вход (еднократно)

Telethon трябва да влезе с твоя номер веднъж. Сесията се записва в `data/signal_copier.session` – пази я като парола.

```bash
docker compose build
docker compose run --rm -it signal-copier login
# пита за кода, който Telegram ти праща (и 2FA паролата, ако имаш)
# накрая показва "channel ok: …" за всеки канал от config.yaml
```

Без Docker: `python -m venv .venv && . .venv/bin/activate && pip install -e . && python -m signal_copier login`.

Проверка на OANDA връзката (за `paper`/`live`): `docker compose run --rm signal-copier check`.

## Стартиране и деплой на VPS

```bash
docker compose up -d --build       # стартира във фонов режим, рестартира се сам
docker compose logs -f             # JSON логове
```

С systemd (стартира при boot):

```bash
sudo mkdir -p /opt && sudo cp -r . /opt/signal-copier   # или clone директно там
sudo cp deploy/signal-copier.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now signal-copier
journalctl -u signal-copier -f
```

Има и `deploy/signal-copier-venv.service` за вариант без Docker.

Смяна на режим: редактирай `mode` в `config.yaml` (и за `live` – `LIVE_CONFIRM=yes` в `.env`), после `docker compose up -d`.

## Команди към твоя бот

| Команда | Действие |
|---|---|
| `/status` | режим, активен/спрян, сигнали днес, отворени позиции, баланс и % за деня |
| `/stop` | kill switch: спира **нови** сделки. Отворените сделки и командите за тях (BE, close) продължават. |
| `/resume` | пуска отново (дневният лимит остава активен до утре) |

Известия получаваш за: нов сигнал, отворена/затворена сделка, отхвърлен сигнал с причина, несигурно съобщение, грешки, достигнат дневен лимит.

## Данни

SQLite файл `data/signal_copier.sqlite`, таблици: `messages` (всяка версия на всяко съобщение), `signals`, `follow_ups`, `orders`, `trades`, `events` (всяко решение с причина), `state`.

```bash
sqlite3 data/signal_copier.sqlite "select ts, kind, decision, reason, signal_key from events order by id desc limit 20"
```

## Парсер

Правилата са в `src/signal_copier/parser/rules.yaml` (regex-и, синоними на символи → OANDA формат: `GOLD`/`XAUUSD` → `XAU_USD`, `US30` → `US30_USD`, `EURUSD` → `EUR_USD`…). Може да се подаде собствен файл чрез `parser.rules_file`. Реалните примери от канала са тестове в `tests/test_real_samples.py` – добавяй всеки нов формат там.

## Структура

```
src/signal_copier/
  models.py        Signal, Entry, FollowUp, IncomingMessage, ParseResult
  settings.py      config.yaml + .env, проверка на LIVE_CONFIRM
  parser/          текст -> Signal / FollowUp (rules.yaml)
  listener/        Telethon userbot
  risk/            проверки и размер на позицията
  executor/        OANDA v20 клиент + изпълнение, idempotent retry, мониторинг
  storage/         SQLite
  notifier/        твоят бот: известия + /stop /status /resume
  pipeline.py      съобщение -> parse -> record | risk -> execute -> store + notify
  app.py           сглобява всичко според MODE
  __main__.py      python -m signal_copier [run|login|check]
deploy/            systemd unit-и
Dockerfile, docker-compose.yml
```

## Тестове

```bash
pip install -e ".[dev]"
pytest
```

Тестовете на изпълнението вървят срещу фалшив OANDA (`tests/conftest.py`), без мрежа.

## Предстои

- Скрипт за оценка на `record` данните: за всеки записан сигнал изтегля M1 свещи от OANDA и симулира влизане с реално забавяне и спред → печалба/загуба, win rate, максимален drawdown.
