# MaxBot — контекст для разработчика

## Язык общения

Всё общение ведётся **на русском языке**: ответы, объяснения, git-коммиты, комментарии к коду.

---

## О проекте

MaxBot — SaaS-сервис для мессенджера MAX (max.ru). Состоит из трёх основных сервисов:

| Сервис      | Назначение |
|-------------|------------|
| `web`       | FastAPI: REST API + Jinja2-интерфейс на порту 8000 |
| `bot`       | Long-polling бот; обрабатывает события MAX API, управляет верификацией участников |
| `scheduler` | APScheduler: публикует отложенные посты (каждые 30 с) и кикает неверифицированных участников (каждые 60 с) |
| `migrate`   | Разовый контейнер: `alembic upgrade head`, затем завершается |
| `db`        | PostgreSQL 16 |

### Ключевые возможности
- Дублирование постов канал → группа с кнопкой «💬 Прокомментировать»
- Верификация новых участников (captcha-gate): сообщения до верификации удаляются, таймаут → кик
- Приветственный DM после верификации с кнопкой возврата в чат
- WelcomeConfig — самостоятельный конфиг верификации для любой группы без привязки к каналу
- Отложенные посты с Markdown и вложениями

---

## Структура проекта

```
maxbot/
├── bot/
│   ├── main.py        # Точка входа; polling/webhook режим
│   ├── supervisor.py  # Один polling-loop на бота
│   ├── handlers.py    # Обработчики событий: message_created, user_added, bot_started
│   ├── client.py      # Async-клиент MAX Bot API (httpx)
│   ├── buttons.py     # Inline-кнопки и заголовки для группы
│   └── crypto.py      # Fernet-шифрование/дешифрование токенов
├── db/
│   ├── models.py      # SQLAlchemy ORM-модели
│   ├── session.py     # AsyncSessionLocal + engine
│   └── alembic/
│       └── versions/  # 001_initial … 005_welcome_configs
├── scheduler/
│   └── main.py        # publish_due_posts + kick_expired_verifications
├── shared/
│   └── config.py      # Pydantic-настройки (читает .env)
├── web/
│   ├── main.py        # FastAPI app; сидирование admin
│   ├── auth.py        # JWT + bcrypt
│   ├── deps.py        # DI: CurrentUser, DBSession, RateLimit
│   └── routers/
│       ├── api.py     # /api/* (JSON)
│       └── pages.py   # HTML-страницы (Jinja2)
├── docker-compose.yml
├── Makefile
└── .env
```

---

## База данных

Основные модели (`db/models.py`):

| Модель                | Назначение |
|-----------------------|------------|
| `User`                | Пользователь сервиса |
| `Subscription`        | Тарифный план и лимиты |
| `Bot`                 | Бот MAX; `encrypted_token` — Fernet-шифр |
| `ChannelGroupPair`    | Пара канал → группа + встроенная верификация |
| `WelcomeConfig`       | Самостоятельный конфиг верификации (без привязки к каналу) |
| `VerificationRequest` | Запрос верификации участника: `pending → verified / kicked / expired` |
| `ScheduledPost`       | Отложенный пост: `pending → sent / failed` |
| `PostLink`            | Маппинг ID поста в канале → ID сообщения в группе |
| `PollingMarker`       | Курсор long-polling (сохраняется между перезапусками) |
| `EventLog`            | Журнал событий: `info / warning / error` |

Текущая актуальная миграция: **`005_welcome_configs`**.

---

## Продакшен-сервер

| Параметр       | Значение |
|----------------|----------|
| SSH-алиас      | `wmoc-prod` |
| IP / порт      | `213.110.208.173:4795` |
| Пользователь   | `prokos` |
| Путь к проекту | `~/maxbot` |
| Git remote     | `https://github.com/ProKoKos/maxbot.git` |

SSH-конфиг уже настроен в `~/.ssh/config` — подключаться через `ssh wmoc-prod`.

---

## Деплой

### Стандартный деплой сервиса

Алгоритм всегда одинаковый: закоммитить → запушить → на сервере подтянуть + пересобрать нужный сервис.

```bash
# 1. Локально: коммит и push
git add <файлы>
git commit -m "описание на русском"
git push origin main

# 2. На сервере
ssh wmoc-prod "cd ~/maxbot && git pull && docker compose build <сервис> && docker compose up -d <сервис>"
```

**Имена сервисов:** `bot`, `web`, `scheduler`

### Деплой конкретных сервисов

```bash
# Только бот
ssh wmoc-prod "cd ~/maxbot && git pull && docker compose build bot && docker compose up -d bot"

# Только веб
ssh wmoc-prod "cd ~/maxbot && git pull && docker compose build web && docker compose up -d web"

# Только планировщик
ssh wmoc-prod "cd ~/maxbot && git pull && docker compose build scheduler && docker compose up -d scheduler"

# Несколько сервисов сразу
ssh wmoc-prod "cd ~/maxbot && git pull && docker compose build bot scheduler && docker compose up -d bot scheduler"
```

### Деплой с миграцией

> **Важно:** сервис `migrate` собирается из того же `web/Dockerfile`. При добавлении новой миграции нужно пересобрать **и** `migrate`, **и** целевые сервисы.

```bash
ssh wmoc-prod "cd ~/maxbot && git pull && docker compose build migrate web bot scheduler && docker compose up -d"
```

Контейнер `migrate` запустится, выполнит `alembic upgrade head` и завершится; остальные сервисы поднимутся после него.

### Просмотр логов на сервере

```bash
ssh wmoc-prod "cd ~/maxbot && docker compose logs bot --tail=50"
ssh wmoc-prod "cd ~/maxbot && docker compose logs web --tail=50"
ssh wmoc-prod "cd ~/maxbot && docker compose logs scheduler --tail=50"
# Следить в реальном времени:
ssh wmoc-prod "cd ~/maxbot && docker compose logs -f bot"
```

### Статус контейнеров

```bash
ssh wmoc-prod "cd ~/maxbot && docker compose ps"
```

---

## Git-коммиты

Коммиты пишутся на **русском языке** в формате `тип: краткое описание`.

Типы: `feat` (новый функционал), `fix` (исправление), `refactor` (рефакторинг), `docs` (документация), `chore` (инфраструктура).

Примеры:
```
feat: удалять сообщения неверифицированных участников
fix: восстановить фразу «нажмите кнопку ниже» в DM по умолчанию
refactor: вынести WelcomeConfig в отдельную модель
docs: обновить README под текущие возможности
```

---

## Важные нюансы

- **Токены ботов** хранятся в зашифрованном виде (Fernet). Расшифровка — `bot/crypto.py:decrypt_token()`. Ключ — `ENCRYPTION_KEY` из `.env`.
- **MAX API** не поддерживает ограничение прав участников — нет эндпоинта mute/restrict. Вместо этого сообщения неверифицированных пользователей удаляются в `handlers.py:_delete_if_unverified()`.
- **group_link** — ссылка на группу для кнопки возврата в чат. MAX API не возвращает её для приватных групп. Цепочка fallback: MAX API → `ChannelGroupPair.group_link` → ручной ввод в настройках.
- **WelcomeConfig** имеет приоритет над `ChannelGroupPair` при обработке `user_added`. Если для группы есть WelcomeConfig, пара игнорируется.
- **migrate-образ** — отдельный Docker-образ `maxbot-migrate`. При добавлении файлов миграций его нужно явно пересобирать (`docker compose build migrate`), иначе новые миграции не будут найдены.
- **PollingMarker** сохраняет курсор long-polling в БД — перезапуск бота не приводит к повторной обработке событий.
- **Планировщик** обрабатывает максимум 20 постов и 50 верификаций за один запуск, чтобы не перегружать API.
