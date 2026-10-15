# signal-copier

Следи Telegram канал за трейдинг сигнали и ги копира в **IC Markets през MetaTrader 5** (по подразбиране) или в OANDA (v20 REST API). Брокерът се избира с `broker:` в `config.yaml`.

| Режим | Какво прави |
|---|---|
| `record` (по подразбиране) | Само чете, парсва и записва сигналите и командите в SQLite. Никаква търговия. |
| `paper` | Търгува на **демо** акаунт (MT5 demo / OANDA practice). Ботът отказва да тръгне, ако MT5 акаунтът е реален. |
| `live` | Търгува на реален акаунт. Стартира само ако в `.env` има `LIVE_CONFIRM=yes`. |

Препоръчителен ред: седмици в `record` → оценка дали групата е печеливша (скрипт с M1 свещи, предстои) → `paper` → чак тогава `live`.

## Как работи

```
Telegram канал ──► listener/ ──► parser/ ──► risk/ ──► executor/ ──► MT5 (или OANDA)
   (Telethon)        нови +       Signal /     размер,     market/limit,
                     редакции     FollowUp     лимити      SL/TP, retry
                         │            │           │            │
                         └────────────┴─── storage/ (SQLite) ──┘
                                              │
                                     notifier/ ──► твоят Telegram бот (/stop /status)
```

- **Сигнал** (`AUDUSD / Buy now @ … / Target Profit 1 @ … / Target Profit 2 @ … / Stop Loss @ …`, също `Buy Limit Order @`, `Sell Stop Order @`): ако минат всички risk проверки, се отваря **една** сделка със SL и TP = последния TP (TP2), както казва наръчникът на RTED: TP1 е само ниво за наблюдение. С `executor.tp_split: equal` вместо това се отваря по една сделка за всеки TP с равен обем.
- **Сигнал без SL** (средно/дългосрочните сделки с Exposure Proportion от наръчника) не се търгува: получаваш известие и решаваш сам.
- **Команди** (reply към сигнала или със символ): `move SL to BE`, `move SL to 1.2345`, `close`, `close half` / `close 30%`, `cancel`, `TP1 hit`, `SL hit`.
- **Несигурно съобщение** → не се търгува, записва се, получаваш известие с текста.
- **Редактиран сигнал**: ако е изпълнен и SL/TP са сменени → местят се SL/TP на отворените сделки. Ако е бил отхвърлен (напр. без SL) и редакцията го оправя → оценява се отново. Смяна на символ/посока → само известие.
- **Редактирана команда** (напр. каналът редактира „Close…“) → не се изпълнява втори път.

### Risk проверки (`config.yaml → risk`)

Размер по фиксиран % риск от баланса спрямо разстоянието до SL (с конверсия към валутата на акаунта); отхвърля без SL; max отворени позиции и max на символ; дневен лимит на загуба (% от NAV в началото на деня, UTC) → спира нови сделки до утре; закъсняло влизане по наръчника: ако цената е тръгнала **срещу** сигнала, но още не е стигнала SL, се влиза (по-добра цена, обемът се смята по новото по-късо SL разстояние, така че рискът остава същият); ако е тръгнала **към TP** с повече от `max_entry_slippage_sl_fraction` × SL разстоянието или вече е зад SL или TP1, сигналът се пропуска, ако сигналът е по-стар от `max_signal_age_seconds`; проверка на спреда (`max_spread` по символ или % от SL); дедупликация по message_id.

### Без двойни поръчки

Всяка част от поръчката има детерминистично client ID (`sc_<chat>_<msg>_<idx>_<part>`), което се записва в SQLite **преди** изпращане. В MT5 то отива като кратък таг в коментара на поръчката (MT5 позволява до 31 символа), в OANDA като clientExtensions.id. При грешка или липсващ отговор ботът първо търси тага в отворените позиции, чакащите поръчки и историята и праща отново само ако брокерът никога не е получавал поръчката. Requote се повтаря с нова цена. При рестарт: Telethon не преизпълнява пропуснати съобщения, всяко съобщение се дедупликира, а редакции на стари съобщения, които ботът не е виждал, се игнорират.

## IC Markets / MT5: къде върви ботът

Официалната Python библиотека `MetaTrader5` работи **само на Windows** и говори с пуснат MT5 терминал на същата машина. Затова:

- **`record` режим** не ползва брокер и върви навсякъде, включително на Linux VPS с Docker (по-долу).
- **`paper` / `live` с MT5** изискват **Windows VPS** (или Windows компютър, който е включен постоянно) с инсталиран MT5 терминал на IC Markets.

Размерът е в **лотове**: 0.5% риск / (разстояние до SL × стойност на 1.0 движение за 1 лот, взета от MT5 за символа). Пример от канала: AUDUSD, SL 9.1 пипа, баланс 10 000 USD → 0.54 лота в една позиция с TP2 (с `tp_split: equal`: 0.27 + 0.27). Минимум 0.01 лот.

Символите се превръщат в имената на IC Markets: `XAU_USD` → `XAUUSD`, `EUR_USD` → `EURUSD`, `US30_USD` → `US30`, `NAS100_USD` → `USTEC` и т.н. (`mt5.symbol_map` в `config.yaml`). `python -m signal_copier check` показва цените на няколко символа, за да видиш, че имената съвпадат.

### Windows VPS (paper/live с MT5)

1. Инсталирай MT5 от IC Markets, влез в **демо** акаунта и включи бутона **Algo Trading** (иначе поръчките се отказват). В Tools → Options → Expert Advisors разреши *Allow algorithmic trading*.
2. Инсталирай Python 3.11+ (от python.org, с отметка *Add to PATH*) и Git.
3. В PowerShell:
   ```powershell
   git clone https://github.com/dimitrov06/dimitrov06.git; cd dimitrov06\signal-copier
   python -m venv .venv; .\.venv\Scripts\Activate.ps1
   pip install -e ".[mt5]"
   copy .env.example .env; copy config.example.yaml config.yaml   # попълни ги
   python -m signal_copier login     # еднократен вход в Telegram
   python -m signal_copier check     # проверява MT5 връзката, акаунта (demo/real) и символите
   python -m signal_copier run
   ```
   В `.env`: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` (напр. `ICMarketsSC-Demo`), по желание `MT5_PATH` до `terminal64.exe`. В `config.yaml`: `mode: paper`, `broker: mt5`.
4. Автоматичен старт: Task Scheduler → *Create Task* → *Run whether user is logged on or not*, trigger *At startup*, action `C:\…\signal-copier\.venv\Scripts\python.exe -m signal_copier run`, *Start in* = папката `signal-copier`. MT5 терминалът също трябва да се стартира автоматично (сложи го в Startup).

## Първоначална настройка

Нужно ти е: Telegram акаунт, който е член на канала; за `record` – Linux VPS с Docker; за търговия с MT5 – Windows VPS (виж по-горе).

1. **Telegram API ключ** (за userbot-а, чете канала като теб): влез в <https://my.telegram.org> → *API development tools* → създай приложение → вземи `api_id` и `api_hash`.
2. **Твой бот за известия**: в Telegram пиши на `@BotFather` → `/newbot` → вземи токена. Пиши каквото и да е на новия бот, после отвори `https://api.telegram.org/bot<TOKEN>/getUpdates` и вземи `message.chat.id` – това е `NOTIFY_CHAT_ID`. Ботът изпълнява команди само от този chat.
3. **Брокер** (само за `paper`/`live`): за IC Markets MT5 – login, парола и сървър на демо акаунта (виж горе). За OANDA (`broker: oanda`) – practice акаунт → *Manage API Access* → токен и account ID.
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

Проверка на връзката с брокера (за `paper`/`live`): `python -m signal_copier check`.

## Стартиране и деплой на Linux VPS (record режим, или OANDA)

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

Правилата са в `src/signal_copier/parser/rules.yaml` (regex-и, синоними на символи → общ формат: `GOLD`/`XAUUSD` → `XAU_USD`, `US30` → `US30_USD`, `EURUSD` → `EUR_USD`…; брокерът после ги превежда към своите имена). Може да се подаде собствен файл чрез `parser.rules_file`. Реалните примери от канала са тестове в `tests/test_real_samples.py` – добавяй всеки нов формат там.

## Структура

```
src/signal_copier/
  models.py        Signal, Entry, FollowUp, IncomingMessage, ParseResult
  settings.py      config.yaml + .env, проверка на LIVE_CONFIRM
  parser/          текст -> Signal / FollowUp (rules.yaml)
  listener/        Telethon userbot
  risk/            проверки и размер на позицията
  executor/        mt5.py (IC Markets MetaTrader 5) и oanda.py/executor.py (OANDA v20):
                   изпълнение, retry без двойни поръчки, мониторинг на затворени сделки
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

Тестовете на изпълнението вървят срещу фалшив MT5 (`tests/fake_mt5.py`) и фалшив OANDA (`tests/conftest.py`), без мрежа и без терминал.

## Предстои

- Скрипт за оценка на `record` данните: за всеки записан сигнал изтегля M1 свещи от IC Markets (MT5 `copy_rates_range`) и симулира влизане с реално забавяне и спред → печалба/загуба, win rate, максимален drawdown.
