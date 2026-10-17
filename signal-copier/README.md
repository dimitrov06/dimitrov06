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

- **Сигнал** (`AUDUSD / Buy now @ … / Target Profit 1 @ … / Target Profit 2 @ … / Stop Loss @ …`, също `Buy Limit Order @`, `Sell Stop Order @`, `Buy-Stop Order @`, `Buy (Stop Order) @`, `Stop Buy Order @`): ако минат всички risk проверки, се отваря **една** сделка със SL и TP = последния TP (TP2), както казва наръчникът на RTED: TP1 е само ниво за наблюдение. С `executor.tp_split: equal` вместо това се отваря по една сделка за всеки TP с равен обем.
- **Сигнал без SL** (средно/дългосрочните сделки с Exposure Proportion от наръчника) не се търгува: получаваш известие и решаваш сам.
- **Команди** (reply към сигнала или със символ): `move SL to BE`, `move SL to 1.2345`, `close`, `close half` / `close 30%`, `cancel`, `TP1 hit`, `SL hit`. Reply към съобщение, което е reply към сигнала (напр. към „TP1 hit“), също намира сигнала; reply към съобщение, което ботът не е виждал (публикувано преди историята му), не търси по символ (може да е друга сделка). Без reply символът търси само сделки, които още са отворени или чакащи (без значение колко са стари); при повече от една – нищо не се прави и получаваш известие. Ако командата назовава друг символ, а не този на сигнала, на който отговаря (напр. „Close XAUUSD“ като reply към AUDUSD), не се прави нищо (`symbol_mismatch`). Една команда за два символа на един ред („Close AUDUSD and XAUUSD“) е несигурна; на отделни редове – изпълнява се за всеки.
- **Команда е само в повелителна форма**: `Close …` в началото на реда или с обект (`close now`, `close XAUUSD`, `close all`, `‼️ Close AUDUSD Manually now at …`). Описателен текст („very close to TP1“, „weekly close above 4400“, „market close soon“, „exit strategy“) се пренебрегва. Команда с отрицание („Don't close yet“, „Don't cancel your order“), с условие или план („move SL to BE once TP1 is hit“, „close half at TP1“, „we will…“, „if…“) или неясна („Closing XAUUSD“, „Close at 23:00“, „Gold could break even higher“) **не се изпълнява**: съобщението е несигурно и получаваш известие с текста.
- **Частично затваряне**: `close half`, `close 30%`, `Close AUDUSD 50% now at …`, `Partially close AUDUSD`, `Close AUDUSD partially at …`, `Close 1/2 of AUDUSD`, `Secure 50% profit`. Процентът на печалбата („(+1.43%)“, „14% profit“) не е размер; цял процент в скоби при пълно затваряне („Close AUDUSD (50%)“) е несигурен.
- **Обновление на сделка не е нов сигнал**: блок с посока и нива, но без входен ред (`Buy now @`, `… Order @`, `@ цена`) е за вече отворена сделка. „XAUUSD Buy - New Stop Loss @ 4475“ / „XAUUSD Buy move SL 4475“ → само `move SL`; „XAUUSD Buy running ✅ TP1 @ … reached, move SL to BE“ → само BE; „XAUUSD Buy update / TP1 @ … hit ✅ / Stop Loss @ … (moved to entry)“ или „Update the Stop Loss @ …“ → несигурно. Reply, който повтаря нива без входен ред, и посока + SL без TP и без вход → несигурно. Нов пост с цял сигнал и „Cancel…“, „Close now“, „Closed manually…“ или „TP1 @ … ✅“ (репост, отмяна с цитат на поръчката) не се търгува (несигурно), и бърза редакция на такъв пост също не го пуска.
- **Тип поръчка**, който парсерът не познава (`Buy Stp Order @`, `Sell Limt Order @`, `Buy Stop Limit @`, `Buy Order @`), **никога** не става пазарна поръчка: съобщението е несигурно.
- **SL команди**: SL се мести само към цената (по-малък риск), никога по-далеч: `move SL to <по-далечна цена>` или редакция, която разширява SL, остават без ефект и получаваш ⚠️. SL с разстояние вместо цена („move SL to 20 pips in profit“, „SL to 1:1“, „50%“, „SL to entry +5 pips“, „trail SL 20 pips“) е несигурно съобщение. Ако BE (или новата цена) още не е възможно, защото цената е зад входа, SL остава и ботът го премества сам, щом цената позволи (⏳).
- **Несигурно съобщение** → не се търгува, записва се, получаваш известие с текста.
- **Редактиран сигнал**: ако е изпълнен и SL/TP са сменени → местят се само сменените нива на отворените сделки (SL, преместен на BE от команда, остава на BE; по-широк SL не се слага). Бележки като „TP1 hit ✅ 4485.10“ под TP редовете не са ново ниво. Ако редакцията добави команда в самия пост („CANCELLED“, „Close now“, „move SL to BE“), тя се изпълнява веднъж за този сигнал. Ако е бил отхвърлен, се оценява отново **само** ако причината се оправя с редакция (липсващ SL, несигурно съобщение) и постът е по-нов от `max_signal_age_seconds` (броено от публикуването, не от редакцията); иначе получаваш ⚠️ и не се търгува. Сигнал, отказан от брокера или с грешка, не се праща отново. Редакция обратно към по-ранен текст (TP2 4498.30 → 4500.30 → 4498.30) е нова версия, не повторна доставка: и тя се прилага. Смяна на символ/посока → само известие.
- **Редактирана команда** (напр. каналът редактира „Close…“) → не се изпълнява втори път (получаваш ℹ️). Ако първият опит е завършил с грешка (❗), редакцията го опитва пак; частично затваряне – никога. Редактирана `move SL to <цена>` с нова цена се прилага (без да разхлабва SL).

### Risk проверки (`config.yaml → risk`)

Размер по фиксиран % риск от баланса (или от equity, ако е по-малко заради плаваща загуба) спрямо разстоянието до SL (с конверсия към валутата на акаунта и `commission_per_lot` – комисионна за лот на акаунти с комисионна); отхвърля без SL; max отворени позиции и max на символ; дневен лимит на загуба (% от NAV в началото на деня, UTC) → спира нови сделки до утре и **отменя чакащите поръчки** (опитва пак всеки рунд, ако MT5 не отговори); с `cap_to_daily_budget: true` (по подразбиране) нова сделка рискува най-много колкото е останало от дневния лимит (при -4% и лимит 6% – 2%), а под минималния обем се отхвърля (`daily_risk_budget`); закъсняло влизане по наръчника: ако цената е тръгнала **срещу** сигнала, но още не е стигнала SL (и Bid/Ask от другата страна също), се влиза (по-добра цена, обемът се смята по новото по-късо SL разстояние, така че рискът остава същият, но най-много до ~3× нормалния обем: `late_entry_min_sl_fraction`); ако е тръгнала **към TP** с повече от `max_entry_slippage_sl_fraction` × SL разстоянието или вече е зад SL или TP1, сигналът се пропуска, ако сигналът е по-стар от `max_signal_age_seconds`; проверка на спреда (`max_spread` по символ или % от SL); обем над `volume_max` на символа или над `max_lots` (по символ) се отхвърля; повторен опит след requote не се праща, ако цената е избягала и рискът би бил >10% над планирания; дедупликация по message_id.

`max_open_positions` брои само **отворените позиции**: чакащите LIMIT/STOP поръчки не заемат място (`count_pending_in_max_open: false`, по подразбиране). Затова чакаща поръчка, която се изпълни по-късно, може да вдигне отворените над лимита. С `count_pending_in_max_open: true` се броят и чакащите. Поръчка в неизвестно състояние (без отговор от MT5) винаги се брои, докато не се появи в MT5 или не се откаже (виж „Без двойни поръчки“). `max_positions_per_symbol` винаги брои и чакащите (без натрупване на дубли).

### Без двойни поръчки

Всяка част от поръчката има детерминистично client ID (`sc_<chat>_<msg>_<idx>_<part>`), което се записва в SQLite **преди** изпращане. В MT5 то отива като кратък таг в коментара на поръчката (MT5 позволява до 31 символа), в OANDA като clientExtensions.id. При грешка или липсващ отговор ботът първо търси тага в отворените позиции, чакащите поръчки и историята. В OANDA праща отново само ако брокерът никога не е получавал поръчката. В MT5 след липсващ отговор или timeout търси тага около 15 s и **никога не я праща отново**: ако не я види, получаваш ❗, поръчката остава „неизвестна“ и мониторът я поема, щом се появи в MT5 (като позиция, чакаща поръчка или вече затворена сделка). Close/cancel/BE/SL команда за сигнал с неизвестна поръчка не се губи: получаваш ⏳ и мониторът я изпълнява, щом поръчката се появи (или я приключва, ако поръчката се откаже). Неизвестна поръчка заема място в `max_open_positions` и на символа. Ако MT5 не я показва никъде (позиции, чакащи поръчки, сделки) `executor.unknown_order_expiry_minutes` (10) минути подред, при отговорил MT5 и терминал с връзка към брокера, се приема за неизпълнена: отбелязва се като неуспешна, мястото се освобождава и получаваш ⌛. Неуспешно четене или терминал без връзка започват чакането отначало; ако поръчката все пак се появи по-късно, ботът я поема. Ако MT5 не отговори на проверка (IPC грешка), това никога не се приема за „няма позиция“. Requote се повтаря с нова цена. При рестарт: съобщенията, публикувани докато ботът е бил спрян (след последното записано), се изтеглят от историята и се обработват по ред с истинската им дата – старите сигнали се отхвърлят по възраст, а close/cancel/BE командите се изпълняват; получаваш ⚠️ с броя. Ако са над 200 или най-старото е на повече от 48 h, не се обработват автоматично, само получаваш известие. Всяко съобщение се дедупликира, а редакции на стари съобщения, които ботът не е виждал, се игнорират. Чакаща поръчка, която се е изпълнила и затворила между две проверки на монитора (или докато ботът е бил спрян), се намира по сделката ѝ в историята: получаваш ✅ изпълнена и 🏁 затворена с P/L.

## IC Markets / MT5: къде върви ботът

Официалната Python библиотека `MetaTrader5` работи **само на Windows** и говори с пуснат MT5 терминал на същата машина. Затова:

- **`record` режим** не ползва брокер и върви навсякъде, включително на Linux VPS с Docker (по-долу).
- **`paper` / `live` с MT5** изискват **Windows VPS** (или Windows компютър, който е включен постоянно) с инсталиран MT5 терминал на IC Markets.

Размерът е в **лотове**: 0.5% риск / (разстояние до SL × стойност на 1.0 движение за 1 лот, взета от MT5 за символа). Пример от канала: AUDUSD, SL 9.1 пипа, баланс 10 000 USD → 0.54 лота в една позиция с TP2 (с `tp_split: equal`: 0.27 + 0.27). Минимум 0.01 лот.

Символите се превръщат в имената на IC Markets: `XAU_USD` → `XAUUSD`, `EUR_USD` → `EURUSD`, `US30_USD` → `US30`, `NAS100_USD` → `USTEC` и т.н. (`mt5.symbol_map` в `config.yaml`). `python -m signal_copier check` показва цените на няколко символа, за да видиш, че имената съвпадат.

### Windows VPS (paper/live с MT5)

1. Инсталирай MT5 от IC Markets, влез в **демо** акаунта и включи бутона **Algo Trading** (иначе поръчките се отказват). В Tools → Options → Expert Advisors разреши *Allow algorithmic trading*. В `paper`/`live` ботът отказва да стартира (и ти пише), ако Algo Trading е изключен, акаунтът не може да търгува или не е hedging; ако Algo Trading се изключи по време на работа, получаваш 🚫.
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
4. Автоматичен старт: Task Scheduler → *Create Task* → trigger *At log on* (за същия потребител, под който върви MT5), action `C:\…\signal-copier\.venv\Scripts\python.exe -m signal_copier run`, *Start in* = папката `signal-copier`. MT5 терминалът също трябва да се стартира автоматично (сложи го в Startup). Ако при старт MT5 или мрежата още не са готови (вкл. терминал, който още не се е свързал с брокера), или ботът падне по-късно, `run` сам се рестартира след 10 s, 30 s, 1, 2, 5 min (Task Scheduler не рестартира задача, която завършва с грешка). Грешка в настройките (демо/реален акаунт, Algo Trading изключен, Telegram не е влязъл) спира бота окончателно с код 2.

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
   Ако каналът е частен и няма @username, използвай числовото му id (`-100…`, най-сигурно; `login` го показва). Можеш да го видиш и в Telegram Desktop: Settings → Advanced → Experimental settings → Show peer IDs. Може и името на групата: тогава при първия старт ботът намира чата с точно това име (или чийто име го съдържа) и запомня id-то му, така че след преименуване следи същия чат (получаваш ℹ️), а не друг със сходно име. При старт получаваш „👂 Следя: <име> (<id>)“.

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
| `/stop` | kill switch: спира **нови** сделки. Отворените сделки и командите за тях (BE, close) продължават. Чакащите поръчки остават в MT5 (отговорът ги изброява, отмени ги ръчно, ако не ги искаш). |
| `/resume` | пуска отново (дневният лимит остава активен до утре) |

Известия получаваш за: нов сигнал, отворена/затворена сделка, отхвърлен сигнал с причина, несигурно съобщение, грешки, достигнат дневен лимит. Известията се пращат на заден план: бавен или блокирал Bot API не забавя сделките.

## Данни

SQLite файл `data/signal_copier.sqlite`, таблици: `messages` (всяка версия на всяко съобщение), `signals`, `follow_ups`, `orders`, `trades`, `events` (всяко решение с причина), `state`.

```bash
sqlite3 data/signal_copier.sqlite "select ts, kind, decision, reason, signal_key from events order by id desc limit 20"
```

## Парсер

Правилата са в `src/signal_copier/parser/rules.yaml` (regex-и, синоними на символи → общ формат: `GOLD`/`XAUUSD` → `XAU_USD`, `US30` → `US30_USD`, `EURUSD` → `EUR_USD`…; брокерът после ги превежда към своите имена). Може да се подаде собствен файл чрез `parser.rules_file`; той трябва да има всички секции на вградения (`not_commands`, `command_guards`, `command_words`, `results`, `update_words`, `pending_hint`…). По-стар файл без тях не се зарежда (ботът спира с грешка в конфигурацията), защото с него „very close to TP1“ би затворило сделката: копирай вградения файл и добави промените си в него. Реалните примери от канала са тестове в `tests/test_real_samples.py` – добавяй всеки нов формат там.

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

Тестовете на изпълнението вървят срещу фалшив MT5 (`tests/fake_mt5.py`) и фалшив OANDA (`tests/conftest.py`), без мрежа и без терминал. `tests/test_e2e_mt5.py` пуска цялото приложение, както го сглобява `run` (config.yaml с твоите настройки, SQLite файл, монитор, известия), срещу симулиран IC Markets EUR акаунт с 990 EUR: цената се движи, чакащите поръчки се изпълняват, SL/TP затварят с P/L в EUR; сценариите са с реалните текстове на канала.

## Предстои

- Скрипт за оценка на `record` данните: за всеки записан сигнал изтегля M1 свещи от IC Markets (MT5 `copy_rates_range`) и симулира влизане с реално забавяне и спред → печалба/загуба, win rate, максимален drawdown.
