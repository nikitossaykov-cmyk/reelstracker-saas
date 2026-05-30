# ReelsTracker SaaS — Handover

Краткий ввод в проект для второго ассистента/коллаборатора.

## Что это

SaaS-трекер метрик Instagram Reels с мульти-юзерной поддержкой. Юзер либо
добавляет рилсы поштучно, либо подключает целый Instagram-аккаунт — сервис
парсит метрики (просмотры/лайки/комменты), пишет историю, рисует динамику,
шлёт уведомления в Telegram при «вирусном» росте.

- **Репо:** https://github.com/nikitossaykov-cmyk/reelstracker-saas (ветка `main`)
- **Деплой:** Railway (Dockerfile + auto-deploy с `main`); локально — есть `docker-compose.yml`
- **Лицензия / приватность:** private repo, single-tenant сейчас

## Стек

- **Backend:** Python 3.10+, FastAPI 0.109, SQLAlchemy 2.0, Uvicorn
- **БД:** PostgreSQL 16 (на Railway — managed Postgres; локально — `postgres:16-alpine` через compose или brew)
- **Миграции:** Alembic-каталог есть, но в проде используется **idempotent inline-миграция** через `ALTER TABLE … ADD COLUMN IF NOT EXISTS` в `app/main.py` → `run_lightweight_migrations()` (вызывается в startup-lifespan). Alembic — для крупных схемных операций (сейчас почти не используется)
- **Auth:** JWT (access + refresh), bcrypt-хэш паролей (`python-jose`, `passlib`)
- **Парсинг IG:**
  - **Apify** (предпочтительно) — actors `apify~instagram-post-scraper` (рилсы) и `apify~instagram-profile-scraper` (профиль). Токен хранится per-user в `users.apify_token`
  - Fallback на прямой парсинг: `requests` → IG Mobile API `i.instagram.com/api/v1/...` (с куки из `accstg.txt`), GraphQL, HTML-скрейп, Selenium через ChromeDriver. Direct paths нестабильны из-за бот-защиты, на них рассчитывать не стоит.
- **Frontend:** один файл `static/tracker.html` — ванильный JS + Tailwind CDN. Без билдов, без npm. Графики на canvas (свой `drawSparkline()`).
- **Внешние сервисы:** Apify API (платный per-call), Telegram Bot API, `wsrv.nl` (прокси для IG-CDN картинок чтобы обойти Referer-блок), `unavatar.io/instagram/{handle}` (fallback для аватаров).

## Как запустить локально (Mac)

Полностью без Docker, Postgres через brew:

```bash
brew install postgresql@16
brew services start postgresql@16
createdb reelstracker

git clone https://github.com/nikitossaykov-cmyk/reelstracker-saas.git
cd reelstracker-saas
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# подставить DATABASE_URL=postgresql://<your_user>@localhost:5432/reelstracker
# поставить SECRET_KEY (любая длинная строка)

python run.py
# uvicorn запускается на http://localhost:8000 с reload
```

Альтернатива — docker-compose (если есть Docker Desktop):

```bash
docker compose up --build
```

На старте автоматически:
1. Создаются таблицы (`Base.metadata.create_all`)
2. Прогоняются inline-миграции (новые колонки `IF NOT EXISTS`)
3. Сбрасываются зависшие RUNNING-задачи старше 10 мин
4. `reattach_orphan_reels()` — спасает рилсы, потерявшие привязку к аккаунту
5. `cleanup_duplicate_reels()` — обнуляет позиции у дублей
6. Стартуют 2 фоновых треда: **scheduler** (раз в 30с проверяет, кому пора парсить) и **parser worker** (раз в 5с забирает задачу из очереди)

## Структура

```
app/
  main.py                 — FastAPI app + lifespan (миграции, треды)
  config.py               — Settings из .env (pydantic-settings)
  database.py             — engine + SessionLocal + Base
  models/
    user.py               — User (email, hashed_password, tariff, telegram_*, apify_token)
    reel.py               — Reel + ReelHistory (current metrics + snapshots)
    account.py            — InstagramAccount (привязан к user, владеет рилсами)
    parsing.py            — ParseJob (очередь, status PENDING/RUNNING/COMPLETED/FAILED;
                            job_type PARSE_REEL | SYNC_ACCOUNT)
  schemas/                — Pydantic-схемы (request/response)
  api/
    auth.py               — /api/auth/register, /login, /refresh, /me
    reels.py              — /api/reels CRUD (только manual-added; ?include_accounts=1 чтобы увидеть всё)
    accounts.py           — /api/accounts CRUD + /sync, /reels
    dashboard.py          — /api/dashboard/* — агрегаты
    parsing.py            — /api/parse/status, force-парсинг
    tariff.py             — /api/tariff (Free/Pro)
    telegram.py           — /api/settings/telegram
    settings_apify.py     — /api/settings/apify (PUT/DELETE токена)
  services/
    parsing_service.py    — create_parse_job, create_account_sync_job, complete/fail
    tariff_service.py     — лимиты (интервал парсинга, кол-во рилсов)
    telegram_service.py   — отправка нотификаций
    reel_service.py       — CRUD рилсов
  core/
    reels_parser.py       — главный парсер. Класс ReelsParser:
                            fetch_reels_via_apify(), fetch_profile_via_apify(),
                            fetch_instagram_profile(), fetch_instagram_reels_list(),
                            parse_reel(url) — для одиночного рилса
  workers/
    scheduler.py          — фоновый поток: schedule_user_reels, schedule_account_syncs (раз в час)
    parser_worker.py      — фоновый поток: process_one_job → _process_parse_reel_job |
                            _process_sync_account_job
static/
  tracker.html            — весь UI (login, dashboard, reels tab, accounts tab, charts, settings)
  login.html
```

## Какие данные парсятся и где хранятся

### Reel (одна строка = один рилс)

| Поле | Что |
|---|---|
| `id, user_id, instagram_account_id` | владелец + (опц.) аккаунт-источник |
| `url, platform` | `instagram`/`tiktok`/`youtube`/`vk` (живой только IG) |
| `title, enabled` | имя в UI + on/off трекинга |
| `views, likes, comments, shares` | **текущие** метрики (последний снэпшот) |
| `thumbnail_url, author_username, author_full_name` | обложка + автор |
| `published_at, caption, duration_seconds` | дата публикации, подпись, длина |
| `position_in_account` | номер рилса в аккаунте (1 = самый свежий) |
| `last_parsed_at, created_at` | таймстемпы |

### ReelHistory

Снэпшоты метрик. На каждый парсинг (если рилс действительно изменился, или это первый раз) пишется новая строка с `views/likes/comments/shares/parsed_at`. Этим строится график динамики и считается `growth.perHour`, по которому определяется «VIRAL».

### InstagramAccount

`instagram_username, instagram_user_id, full_name, profile_pic_url, bio,
followers_count, following_count, posts_count, sync_enabled, last_synced_at,
last_sync_error`. Один username на юзера (UniqueConstraint).

### ParseJob (очередь)

`job_type` (`PARSE_REEL` | `SYNC_ACCOUNT`), `status`, `priority` (Pro=10, Free=0),
ссылки на `reel_id` / `account_id`. Воркер берёт `status=PENDING` с максимальным
`priority`, выставляет `RUNNING`, по завершении — `COMPLETED`/`FAILED`.

## HTTP API

Все эндпоинты под `/api/`, требуют JWT в заголовке `Authorization: Bearer <token>`
(кроме `/auth/register`, `/auth/login`, `/health`).

### Auth
- `POST /api/auth/register` `{email, password}` → `{access_token, refresh_token}`
- `POST /api/auth/login` `{email, password}` → tokens
- `POST /api/auth/refresh` `{refresh_token}` → новый access
- `GET /api/auth/me` → текущий юзер

### Reels (вкладка «Рилсы» — manual)
- `GET /api/reels` — список (по умолчанию **без** account-imported; `?include_accounts=1` для всех)
- `POST /api/reels` `{title, platform, url}` — добавить + сразу в очередь
- `GET /api/reels/{id}` / `PUT /{id}` / `DELETE /{id}`
- `GET /api/reels/{id}/history` — снэпшоты для графика

### Accounts (вкладка «Аккаунты» — bulk-импорт)
- `GET /api/accounts` — список аккаунтов
- `POST /api/accounts` `{username}` — добавить (тянет профиль, ставит первый sync в очередь)
- `GET /api/accounts/{id}` — деталь аккаунта
- `GET /api/accounts/{id}/reels` — рилсы аккаунта (отсортированы по `position_in_account` asc, NULL в конец)
- `POST /api/accounts/{id}/sync` — форс-сync (новая SYNC_ACCOUNT-задача)
- `DELETE /api/accounts/{id}` — рилсы каскадно удаляются (`cascade="all, delete-orphan"` в relationship)

### Settings
- `GET / PUT / DELETE /api/settings/apify` — токен Apify (валидируется префикс `apify_api_`)
- `GET / PUT /api/settings/telegram` — настройки бота + пороги нотификаций
- `POST /api/settings/telegram/test` — тестовое сообщение

### Прочее
- `GET /api/dashboard/*` — агрегаты для главной
- `GET /api/parse/status` — состояние очереди
- `POST /api/tariff/upgrade` — Free → Pro (без оплаты, просто переключатель)
- `GET /health` → `{"status": "ok", "version": "1.0.0"}`

Swagger UI: `GET /docs`, OpenAPI JSON: `GET /openapi.json`.

## Как «дёрнуть» вручную (curl)

```bash
# 1. Регистрация
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"qwerty123"}'

# 2. Логин (если уже зарегистрирован) — берём access_token
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"qwerty123"}' | jq -r .access_token)

# 3. Положить Apify-токен
curl -X PUT http://localhost:8000/api/settings/apify \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"apify_token":"apify_api_XXXXXXXX"}'

# 4. Добавить аккаунт
curl -X POST http://localhost:8000/api/accounts \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"username":"nasa"}'

# 5. Получить рилсы аккаунта (после ~30-60 сек на sync)
curl -X GET http://localhost:8000/api/accounts/1/reels \
  -H "Authorization: Bearer $TOKEN" | jq .
```

## CLI / запуск отдельных скриптов

CLI как такового нет. Если нужно дернуть парсер вручную:

```python
# python3 -i (внутри проекта, с активным .venv)
from app.core.reels_parser import ReelsParser
p = ReelsParser()
print(p.fetch_reels_via_apify("nasa", "apify_api_XXX", results_limit=10))
print(p.parse_reel("https://www.instagram.com/reel/Cxxxxxxxxxx/"))
```

## Известное и недоделанное

**Стабильность парсинга:**
- Прямой парсинг IG (без Apify) практически мёртв — IG агрессивно блокирует. На Apify надо иметь баланс ($5 trial у Apify обычно хватает на тысячи запросов).
- Selenium fallback требует локально установленный Chrome; путь определяется кросс-платформенно, но в Docker-образе Chrome нет. На Railway Selenium не работает.

**Бизнес-логика, что не доделано:**
- **Биллинг.** Тариф Pro/Free есть, но Stripe/ЮКасса не подключены — `upgrade` просто меняет колонку.
- **Reset password / email confirmation.** Регистрация без подтверждения email.
- **TikTok / YouTube / VK** — поля в модели есть, парсера нет.
- **Удаление рилса из аккаунта** через UI — пока нет кнопки удаления конкретного reel в account view (есть только удаление всего аккаунта).
- **Alembic-миграции.** Используется inline-миграция в `main.py`; нормальные `alembic revision`-ы не пишутся, схема расходится с Alembic-каталогом.
- **Тесты.** Нет.
- **CSRF / rate limiting / abuse protection.** Нет — CORS открыт на `*`, JWT-токены не отзываются.

**Текущая UI-новинка (то, что только что добавил claude):**
- В детали аккаунта — toolbar с сортировкой (свежесть/просмотры/дата/лайки/ER) и фильтром «Только VIRAL»
- Под каждым рилсом — мини-спарклайн динамики + стрелка тренда (↑/↓ с цветом)
- Tooltip + подсветка пиков на всех графиках (вкл. сводные графики аккаунта)
- Двухступенчатый fallback для аватаров: `unavatar.io/instagram/{handle}` → wsrv.nl(stored IG-CDN URL) → буква
- Вкладка «Рилсы» теперь чистая — рилсы из аккаунтов туда не попадают (фильтр `instagram_account_id IS NULL`)

**Подводные камни:**
- IG-CDN URL'ы (аватарки + thumbnails) — signed, протухают за пару часов. Решение: всегда тянуть через Apify свежие; на фронте — `unavatar.io` fallback.
- В Apify-ответе один и тот же рилс может прийти под `/p/{sc}/` или `/reel/{sc}/`. Воркер дедупит по shortcode через LIKE-паттерны, URL канонизирует к `/reel/{sc}/`.
- Scheduler-тик каждые 30с, sync-job — раз в час на аккаунт. Если sync падает (Apify timeout), `last_synced_at` не выставится → следующий тик создаст новую задачу, но `create_account_sync_job` дедупит pending/running задачи.

## ENV / секреты

Минимум, что нужно в `.env` чтобы запустить:

```
DATABASE_URL=postgresql://user:pass@host:5432/reelstracker
SECRET_KEY=<длинная случайная строка>
```

Опционально:
- `PROXY_ENABLED=true` + `PROXY_LIST=host:port:user:pass` — для прямого парсинга
- `CHROME_BINARY_PATH`, `CHROMEDRIVER_PATH` — если Selenium не находит браузер автоматически

Apify-токен и Telegram-настройки **per-user** в БД, не через ENV.

## Контакты / куда смотреть в первую очередь

1. **`app/main.py`** — стартап-flow, какие миграции и треды поднимаются
2. **`app/workers/parser_worker.py`** — `_process_sync_account_job` — самая горячая логика, через неё идут все account-syncи
3. **`app/core/reels_parser.py`** — `fetch_reels_via_apify` и `fetch_profile_via_apify` — что именно отправляется в Apify и как парсится ответ
4. **`static/tracker.html`** — весь UI; искать по `openAccount`, `renderAccountGrid`, `drawSparkline`
