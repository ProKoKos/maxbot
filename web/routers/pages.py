"""
Jinja2 HTML page routes.
All pages (except /login) require cookie auth, redirect to /login on failure.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Bot, ChannelGroupPair, EventLog, PostStatus, ScheduledPost, User, WelcomeConfig
from db.session import get_async_session
from shared.config import get_settings
from web.auth import create_access_token, verify_password
from web.deps import DBSession

router = APIRouter()
templates = Jinja2Templates(directory="/app/web/templates")
settings = get_settings()


def _get_user_from_cookie(request: Request) -> str | None:
    from jose import JWTError
    from web.auth import decode_token
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = decode_token(token)
        return payload.get("sub")
    except JWTError:
        return None


async def _require_user(request: Request, session: AsyncSession) -> User:
    email = _get_user_from_cookie(request)
    if not email:
        raise HTTPException(status_code=302, headers={"location": "/login"})
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=302, headers={"location": "/login"})
    return user


# ── Login / Logout ─────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, session: DBSession):
    form = await request.form()
    email = str(form.get("email", "")).strip()
    password = str(form.get("password", ""))

    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Неверный email или пароль"},
            status_code=401,
        )

    token = create_access_token({"sub": user.email})
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(
        "access_token", token, httponly=True, samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
    )
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("access_token")
    return resp


# ── Dashboard ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: DBSession):
    user = await _require_user(request, session)

    pairs_result = await session.execute(
        select(ChannelGroupPair)
        .where(ChannelGroupPair.user_id == user.id)
        .order_by(ChannelGroupPair.created_at.desc())
    )
    pairs = pairs_result.scalars().all()

    bots_result = await session.execute(
        select(Bot).where(Bot.user_id == user.id)
    )
    bots = bots_result.scalars().all()

    logs_result = await session.execute(
        select(EventLog)
        .where(EventLog.user_id == user.id)
        .order_by(EventLog.created_at.desc())
        .limit(5)
    )
    recent_logs = logs_result.scalars().all()

    # Build bot lookup for pairs display
    bot_map = {b.id: b for b in bots}

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "pairs": pairs,
            "bots": bots,
            "bot_map": bot_map,
            "recent_logs": recent_logs,
            "active_page": "dashboard",
        },
    )


# ── Bots ──────────────────────────────────────────────────────────────────────

@router.get("/bots", response_class=HTMLResponse)
async def bots_page(request: Request, session: DBSession):
    user = await _require_user(request, session)

    result = await session.execute(
        select(Bot)
        .where(Bot.user_id == user.id)
        .order_by(Bot.created_at.desc())
    )
    bots = result.scalars().all()

    return templates.TemplateResponse(
        "bots.html",
        {"request": request, "user": user, "bots": bots, "active_page": "bots"},
    )


# ── Pairs management ──────────────────────────────────────────────────────────

@router.get("/pairs", response_class=HTMLResponse)
async def pairs_page(request: Request, session: DBSession):
    user = await _require_user(request, session)

    pairs_result = await session.execute(
        select(ChannelGroupPair)
        .where(ChannelGroupPair.user_id == user.id)
        .order_by(ChannelGroupPair.created_at.desc())
    )
    pairs = pairs_result.scalars().all()

    bots_result = await session.execute(
        select(Bot)
        .where(Bot.user_id == user.id, Bot.is_active == True)  # noqa
        .order_by(Bot.name)
    )
    bots = bots_result.scalars().all()
    bot_map = {b.id: b for b in bots}

    return templates.TemplateResponse(
        "pairs.html",
        {
            "request": request,
            "user": user,
            "pairs": pairs,
            "bots": bots,
            "bot_map": bot_map,
            "active_page": "discussions",
        },
    )


# ── Event log ─────────────────────────────────────────────────────────────────

@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, session: DBSession):
    user = await _require_user(request, session)

    result = await session.execute(
        select(EventLog)
        .where(EventLog.user_id == user.id)
        .order_by(EventLog.created_at.desc())
        .limit(50)
    )
    logs = result.scalars().all()

    return templates.TemplateResponse(
        "logs.html",
        {"request": request, "user": user, "logs": logs, "active_page": "logs"},
    )


# ── Autopost ──────────────────────────────────────────────────────────────────

@router.get("/autopost", response_class=HTMLResponse)
async def autopost_page(request: Request, session: DBSession):
    user = await _require_user(request, session)

    # Only show pairs that have a bot assigned
    pairs_result = await session.execute(
        select(ChannelGroupPair).where(
            ChannelGroupPair.user_id == user.id,
            ChannelGroupPair.enabled == True,  # noqa
            ChannelGroupPair.bot_id.isnot(None),
        )
    )
    pairs = pairs_result.scalars().all()

    posts_result = await session.execute(
        select(ScheduledPost)
        .where(ScheduledPost.user_id == user.id)
        .order_by(ScheduledPost.scheduled_at.desc())
        .limit(50)
    )
    posts = posts_result.scalars().all()

    return templates.TemplateResponse(
        "autopost.html",
        {
            "request": request,
            "user": user,
            "pairs": pairs,
            "posts": posts,
            "PostStatus": PostStatus,
            "active_page": "autopost",
        },
    )


# ── Helpers for placeholder pages ─────────────────────────────────────────────

def _coming_soon(request: Request, user, active_page: str, title: str, icon: str,
                 description: str, features: list[dict]):
    return templates.TemplateResponse(
        "coming_soon.html",
        {
            "request": request,
            "user": user,
            "active_page": active_page,
            "title": title,
            "icon": icon,
            "description": description,
            "features": features,
        },
    )


# ── Контент и каналы ──────────────────────────────────────────────────────────

@router.get("/crosspost", response_class=HTMLResponse)
async def crosspost_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "crosspost",
        title="Кросспостинг",
        icon="🔀",
        description=(
            "Автоматически дублируйте публикации из одного канала MAX в несколько других каналов "
            "одновременно. Идеально для сеток каналов, региональных версий или тематических рубрик."
        ),
        features=[
            {
                "icon": "📡",
                "title": "Публикация в несколько каналов сразу",
                "description": (
                    "Один пост — мгновенная доставка в любое количество выбранных каналов. "
                    "Настройте список получателей один раз: каждая новая публикация разойдётся "
                    "по всей сети без лишних действий с вашей стороны."
                ),
            },
            {
                "icon": "✏️",
                "title": "Индивидуальная адаптация текста",
                "description": (
                    "Для каждого канала-получателя можно задать префикс, суффикс или шаблон "
                    "подстановки: автоматически добавляйте подпись, хэштеги или ссылку на источник, "
                    "не трогая исходный текст."
                ),
            },
            {
                "icon": "⏱️",
                "title": "Задержка между репостами",
                "description": (
                    "Настройте паузу между публикациями в каждый канал — от нескольких секунд "
                    "до часов. Это помогает избежать ощущения спама у подписчиков, которые "
                    "состоят в нескольких ваших каналах."
                ),
            },
            {
                "icon": "🔗",
                "title": "Сохранение форматирования и медиа",
                "description": (
                    "Кросспостинг передаёт не только текст, но и вложения: фото, видео, документы, "
                    "голосовые сообщения. Markdown-разметка и inline-кнопки воспроизводятся "
                    "без искажений в каждом канале."
                ),
            },
            {
                "icon": "🚫",
                "title": "Фильтры и исключения",
                "description": (
                    "Задайте ключевые слова или хэштеги, при наличии которых пост не будет "
                    "кросспоститься в определённые каналы. Удобно, если часть контента "
                    "предназначена только для основной аудитории."
                ),
            },
            {
                "icon": "📊",
                "title": "Статистика охвата",
                "description": (
                    "Видите суммарный охват каждой публикации по всем каналам-получателям: "
                    "просмотры, реакции, клики по кнопкам. Понимайте, какие материалы "
                    "работают лучше всего в рамках вашей сети."
                ),
            },
        ],
    )


@router.get("/rss", response_class=HTMLResponse)
async def rss_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "rss",
        title="RSS → канал",
        icon="📡",
        description=(
            "Подпишите MAX-канал на любой RSS/Atom-фид: новые записи из блогов, новостных сайтов "
            "или подкастов будут автоматически появляться в канале в нужном вам формате."
        ),
        features=[
            {
                "icon": "🔔",
                "title": "Мониторинг фидов в реальном времени",
                "description": (
                    "Система опрашивает RSS/Atom-источники с интервалом от 5 минут. "
                    "Как только появляется новая запись — бот немедленно публикует её в канал, "
                    "чтобы ваши читатели всегда получали свежую информацию первыми."
                ),
            },
            {
                "icon": "🎨",
                "title": "Настраиваемый шаблон поста",
                "description": (
                    "Определите, как выглядит публикация: заголовок, описание, ссылка, изображение "
                    "из og:image. Используйте переменные {{title}}, {{summary}}, {{link}}, {{author}} "
                    "для построения любого нужного формата сообщения."
                ),
            },
            {
                "icon": "🗂️",
                "title": "Несколько источников на один канал",
                "description": (
                    "Подключайте сколько угодно RSS-фидов к одному каналу — агрегируйте новости "
                    "из множества источников в единый поток. Каждый источник настраивается "
                    "независимо: разные шаблоны, разные фильтры."
                ),
            },
            {
                "icon": "🔍",
                "title": "Фильтрация по ключевым словам",
                "description": (
                    "Публикуйте только те записи, которые содержат нужные слова в заголовке "
                    "или описании. Или, наоборот, исключайте записи по стоп-словам — "
                    "чтобы канал оставался тематическим и без информационного шума."
                ),
            },
            {
                "icon": "🕐",
                "title": "Очередь и лимиты",
                "description": (
                    "Задайте максимальное количество постов в час, чтобы при большом потоке "
                    "новостей не заспамить канал. Излишек сохраняется в очереди и публикуется "
                    "равномерно в течение дня."
                ),
            },
            {
                "icon": "📝",
                "title": "История и дедупликация",
                "description": (
                    "Система запоминает все опубликованные записи по GUID фида — один "
                    "и тот же материал никогда не попадёт в канал дважды, даже если "
                    "RSS-источник обновил дату или изменил описание."
                ),
            },
        ],
    )


@router.get("/series", response_class=HTMLResponse)
async def series_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "series",
        title="Серийные публикации",
        icon="📚",
        description=(
            "Создавайте образовательные курсы, марафоны, еженедельные рубрики и любые "
            "серии постов с автоматической публикацией по расписанию — читатели получают "
            "контент в нужное время без ручного вмешательства."
        ),
        features=[
            {
                "icon": "🗓️",
                "title": "Гибкое расписание серии",
                "description": (
                    "Укажите дату старта и периодичность: каждый день, раз в неделю "
                    "в определённый день, дважды в месяц или по любому custom-паттерну. "
                    "Серия публикуется сама — вы только один раз готовите все материалы."
                ),
            },
            {
                "icon": "📝",
                "title": "Редактор эпизодов",
                "description": (
                    "Пишите и редактируйте все части серии заранее в удобном интерфейсе. "
                    "Каждый эпизод — отдельный пост с текстом, медиафайлами и кнопками. "
                    "Меняйте порядок перетаскиванием, вставляйте паузы между выпусками."
                ),
            },
            {
                "icon": "🔢",
                "title": "Автонумерация и навигация",
                "description": (
                    "К каждому эпизоду автоматически добавляется порядковый номер и "
                    "ссылки на предыдущую/следующую части. Новые подписчики легко "
                    "находят начало серии через закреплённый навигационный пост."
                ),
            },
            {
                "icon": "👤",
                "title": "Индивидуальная рассылка подписчикам",
                "description": (
                    "Помимо публикации в канале, каждый эпизод можно отправлять "
                    "персонально в личку подписчикам, которые явно подписались на серию "
                    "через бота — как email-рассылка, только в MAX."
                ),
            },
            {
                "icon": "⏸️",
                "title": "Управление состоянием серии",
                "description": (
                    "Поставьте серию на паузу, чтобы пропустить период, или сдвиньте "
                    "все оставшиеся эпизоды вперёд на n дней одним кликом. Экстренное "
                    "обновление вышедшего эпизода без нарушения общего расписания."
                ),
            },
            {
                "icon": "📈",
                "title": "Аналитика по сериям",
                "description": (
                    "Отслеживайте, сколько читателей дошло до каждого эпизода, "
                    "на каком шаге происходит наибольший отток, какие выпуски собирают "
                    "больше всего реакций — оптимизируйте контент опираясь на данные."
                ),
            },
        ],
    )


@router.get("/ai-gen", response_class=HTMLResponse)
async def ai_gen_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "ai_gen",
        title="AI-генерация",
        icon="✨",
        description=(
            "Создавайте посты для канала с помощью ИИ: задайте тему и тон — "
            "получите готовый текст, адаптированный под вашу аудиторию и стиль канала."
        ),
        features=[
            {
                "icon": "🧠",
                "title": "Генерация по теме и ключевым словам",
                "description": (
                    "Введите тему поста и несколько ключевых слов — AI напишет полноценный "
                    "текст нужной длины с заголовком, основной частью и призывом к действию. "
                    "Поддерживаются стили: экспертный, разговорный, новостной, сторителлинг."
                ),
            },
            {
                "icon": "🎭",
                "title": "Обучение голосу канала",
                "description": (
                    "Загрузите примеры своих лучших постов — AI изучит ваш уникальный стиль, "
                    "лексику и манеру подачи. Все последующие генерации будут звучать так, "
                    "будто их написали вы сами, а не робот."
                ),
            },
            {
                "icon": "🔄",
                "title": "Рерайт и улучшение текста",
                "description": (
                    "Вставьте черновик или источник — AI перепишет его под формат "
                    "канала: сократит, сделает живее, добавит эмодзи и структуру. "
                    "Режим «упростить», «сделать экспертным», «добавить юмор»."
                ),
            },
            {
                "icon": "📅",
                "title": "Контент-план на месяц",
                "description": (
                    "Задайте тематику канала и целевую аудиторию — AI предложит "
                    "контент-план на 30 дней с темами, форматами и оптимальным "
                    "временем публикации. Принимайте, отклоняйте или редактируйте темы."
                ),
            },
            {
                "icon": "🖼️",
                "title": "Генерация подписей к изображениям",
                "description": (
                    "Загрузите фото — AI опишет его и создаст цепляющую подпись "
                    "для публикации. Работает с инфографикой, скриншотами, "
                    "фотографиями мероприятий и продуктовыми снимками."
                ),
            },
            {
                "icon": "⚡",
                "title": "Прямая публикация в канал",
                "description": (
                    "Сгенерированный текст можно сразу опубликовать или отправить "
                    "в планировщик автопостинга — без копирования. Правьте прямо "
                    "в редакторе перед публикацией: AI учтёт правки при следующих генерациях."
                ),
            },
        ],
    )


# ── Аудитория ─────────────────────────────────────────────────────────────────

# Default verification strings — kept in sync with bot/handlers.py
_DEFAULT_VERIFY_MSG = (
    "👋 Привет, {имя}!\n\n"
    "Добро пожаловать в {группа}. Чтобы получить доступ к чату, подтвердите, "
    "что вы не бот — нажмите кнопку ниже.\n\n"
    "⏰ Время на верификацию: {минут} мин."
)
_DEFAULT_VERIFY_BTN = "✅ Я не бот"
_DEFAULT_WELCOME_DM = "✅ Верификация пройдена! Добро пожаловать в {группа}."


@router.get("/welcome", response_class=HTMLResponse)
async def welcome_page(request: Request, session: DBSession):
    user = await _require_user(request, session)

    configs_result = await session.execute(
        select(WelcomeConfig)
        .where(WelcomeConfig.user_id == user.id)
        .order_by(WelcomeConfig.created_at.desc())
    )
    configs = configs_result.scalars().all()

    bots_result = await session.execute(
        select(Bot).where(Bot.user_id == user.id)
    )
    bots = bots_result.scalars().all()
    bot_map = {b.id: b for b in bots}

    # Serialize configs to dicts for JSON embedding in the template
    configs_data = [
        {
            "id": c.id,
            "group_name": c.group_name or "",
            "group_id": c.group_id,
            "group_link": c.group_link or "",
            "bot_id": c.bot_id,
            "bot_name": (
                f"@{bot_map[c.bot_id].max_username or bot_map[c.bot_id].name}"
                if c.bot_id and c.bot_id in bot_map
                else ""
            ),
            "verification_enabled": c.verification_enabled,
            "verification_timeout_min": c.verification_timeout_min,
            "verification_message": c.verification_message or "",
            "verification_button_text": c.verification_button_text or "",
            "verification_kick": c.verification_kick,
            "verification_notify_success": c.verification_notify_success,
            "verification_welcome_dm": c.verification_welcome_dm or "",
        }
        for c in configs
    ]

    bots_list = [
        {
            "id": b.id,
            "name": (
                f"{b.name} (@{b.max_username})" if b.name and b.max_username
                else b.name or f"@{b.max_username}"
            ),
        }
        for b in bots
        if b.is_active
    ]

    return templates.TemplateResponse(
        "welcome.html",
        {
            "request": request,
            "user": user,
            "configs_data": configs_data,
            "bots_list": bots_list,
            "active_page": "welcome",
            "default_verify_msg": _DEFAULT_VERIFY_MSG,
            "default_verify_btn": _DEFAULT_VERIFY_BTN,
            "default_welcome_dm": _DEFAULT_WELCOME_DM,
        },
    )


@router.get("/moderation", response_class=HTMLResponse)
async def moderation_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "moderation",
        title="Модерация",
        icon="🛡️",
        description=(
            "Автоматически фильтруйте спам, нежелательный контент и нарушителей правил "
            "в группах обсуждений — держите сообщество чистым без ручной работы."
        ),
        features=[
            {
                "icon": "🚫",
                "title": "Фильтрация по стоп-словам",
                "description": (
                    "Задайте список запрещённых слов, фраз и регулярных выражений. "
                    "Сообщения, содержащие их, автоматически удаляются, а автор "
                    "получает предупреждение или временный бан в зависимости от настроек."
                ),
            },
            {
                "icon": "🤖",
                "title": "AI-модерация контента",
                "description": (
                    "Языковая модель анализирует сообщения на токсичность, оскорбления, "
                    "угрозы и рекламный спам — даже если явных стоп-слов нет. "
                    "Настраиваемый порог чувствительности под специфику вашего сообщества."
                ),
            },
            {
                "icon": "🔗",
                "title": "Антиспам и антифлуд",
                "description": (
                    "Блокировка ссылок от не-администраторов, удаление пересланных сообщений "
                    "из нежелательных источников, ограничение частоты постов от одного "
                    "участника за период времени."
                ),
            },
            {
                "icon": "⚖️",
                "title": "Система предупреждений (warn)",
                "description": (
                    "Накопительная система: первое нарушение — предупреждение, второе — "
                    "временное ограничение на отправку, третье — бан. Все действия "
                    "логируются, администратор может видеть историю нарушений любого участника."
                ),
            },
            {
                "icon": "👮",
                "title": "Команды модераторов",
                "description": (
                    "Назначайте доверенных участников модераторами: они получают "
                    "доступ к командам /warn, /mute, /ban прямо в чате. "
                    "Все действия фиксируются в журнале с указанием исполнителя."
                ),
            },
            {
                "icon": "📣",
                "title": "Уведомления в канал управления",
                "description": (
                    "Все события модерации (удалённые сообщения, выданные баны) "
                    "дублируются в приватный канал администратора с полным контекстом: "
                    "текст нарушения, аватар и ник нарушителя, применённое действие."
                ),
            },
        ],
    )


@router.get("/polls", response_class=HTMLResponse)
async def polls_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "polls",
        title="Опросы",
        icon="📊",
        description=(
            "Создавайте интерактивные опросы, голосования и квизы прямо в канале или группе — "
            "вовлекайте аудиторию и собирайте мнения без сторонних инструментов."
        ),
        features=[
            {
                "icon": "✅",
                "title": "Одиночный и множественный выбор",
                "description": (
                    "Простые опросы с одним правильным ответом или голосования, "
                    "где участник выбирает несколько вариантов. "
                    "Результаты обновляются в реальном времени и видны всем участникам."
                ),
            },
            {
                "icon": "🔒",
                "title": "Анонимные голосования",
                "description": (
                    "Включите анонимный режим — участники голосуют, не опасаясь "
                    "огласки. Вы видите только агрегированные результаты. "
                    "Полезно для откровенных опросов о качестве контента."
                ),
            },
            {
                "icon": "🧩",
                "title": "Квизы с проверкой ответов",
                "description": (
                    "Создайте викторину: задайте правильный ответ — бот "
                    "покажет участнику, угадал ли он, и выведет объяснение. "
                    "Идеально для образовательных каналов и конкурсов."
                ),
            },
            {
                "icon": "⏱️",
                "title": "Опросы с ограничением по времени",
                "description": (
                    "Задайте срок голосования — через указанное время опрос "
                    "автоматически закрывается и в канале публикуется итоговая "
                    "сводка с результатами и инфографикой."
                ),
            },
            {
                "icon": "📅",
                "title": "Плановые и повторяющиеся опросы",
                "description": (
                    "Запускайте опросы по расписанию: ежедневный вопрос дня, "
                    "еженедельный рейтинг лучшего поста, ежемесячный NPS. "
                    "Настройте один раз — система проводит регулярно автоматически."
                ),
            },
            {
                "icon": "📤",
                "title": "Экспорт результатов",
                "description": (
                    "Скачайте детальный отчёт по опросу в CSV: когда проголосовал "
                    "каждый участник, какой вариант выбрал. Анализируйте "
                    "данные в Excel или Google Sheets."
                ),
            },
        ],
    )


@router.get("/gamification", response_class=HTMLResponse)
async def gamification_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "gamification",
        title="Геймификация",
        icon="🏆",
        description=(
            "Превратите участие в вашем сообществе в игру: очки, уровни, бейджи и "
            "рейтинги мотивируют аудиторию быть активной и возвращаться снова и снова."
        ),
        features=[
            {
                "icon": "⭐",
                "title": "Система очков и уровней",
                "description": (
                    "Участники получают очки за полезные действия: комментарий, "
                    "реакция на пост, участие в опросе, приглашение друга. "
                    "По мере накопления очков растёт уровень — от «Новичка» до «Эксперта»."
                ),
            },
            {
                "icon": "🏅",
                "title": "Достижения и бейджи",
                "description": (
                    "Создайте собственный набор значков для особых событий: "
                    "«Первый комментарий», «100 дней в сообществе», «Топ-автор недели». "
                    "Бейджи отображаются рядом с именем участника в его профиле."
                ),
            },
            {
                "icon": "📋",
                "title": "Таблица лидеров",
                "description": (
                    "Автоматически публикуемый рейтинг самых активных участников "
                    "за неделю/месяц/всё время. Здоровая конкуренция стимулирует "
                    "вовлечённость и удерживает аудиторию."
                ),
            },
            {
                "icon": "🎁",
                "title": "Награды за активность",
                "description": (
                    "Привяжите достижение порогов к реальным поощрениям: промокод, "
                    "доступ к закрытому каналу, персональная консультация. "
                    "Бот выдаёт награды автоматически при достижении условий."
                ),
            },
            {
                "icon": "🎯",
                "title": "Задания и челленджи",
                "description": (
                    "Запускайте временные задания: «оставь 5 комментариев за эту "
                    "неделю и получи двойные очки». Используйте для оживления "
                    "активности в спокойные периоды или во время запусков."
                ),
            },
            {
                "icon": "🛒",
                "title": "Магазин наград",
                "description": (
                    "Участники тратят накопленные очки на товары из вашего каталога: "
                    "эксклюзивный контент, скидки, мерч, услуги. Полный контроль "
                    "над ассортиментом и стоимостью каждой позиции."
                ),
            },
        ],
    )


# ── Чат-бот ───────────────────────────────────────────────────────────────────

@router.get("/faq", response_class=HTMLResponse)
async def faq_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "faq",
        title="FAQ",
        icon="❓",
        description=(
            "Создайте базу знаний с ответами на частые вопросы — бот автоматически "
            "распознаёт вопрос пользователя и отвечает нужным материалом."
        ),
        features=[
            {
                "icon": "📚",
                "title": "Редактор базы знаний",
                "description": (
                    "Добавляйте вопросы и ответы в удобном интерфейсе. "
                    "Группируйте по категориям, добавляйте медиафайлы и кнопки. "
                    "Импорт из Google Sheets или CSV для быстрого старта."
                ),
            },
            {
                "icon": "🔍",
                "title": "Семантический поиск",
                "description": (
                    "Бот понимает смысл вопроса, а не только ключевые слова. "
                    "«Как отменить подписку» и «хочу уйти» приведут к одному ответу. "
                    "AI-embeddings обеспечивают точность без сложной настройки синонимов."
                ),
            },
            {
                "icon": "💬",
                "title": "Уточняющие вопросы",
                "description": (
                    "Если запрос неоднозначен, бот предлагает варианты: "
                    "«Вы имеете в виду А или Б?» — и переходит к нужному разделу. "
                    "Диалоговая навигация упрощает поиск для пользователя."
                ),
            },
            {
                "icon": "🔄",
                "title": "Эскалация на живого оператора",
                "description": (
                    "Если бот не нашёл подходящего ответа или пользователь "
                    "явно просит связаться с человеком — запрос передаётся "
                    "в Helpdesk-очередь с полным контекстом переписки."
                ),
            },
            {
                "icon": "📈",
                "title": "Аналитика вопросов",
                "description": (
                    "Видите, какие вопросы задают чаще всего, на какие бот "
                    "не смог ответить, сколько запросов обработано без участия человека. "
                    "Данные помогают расширять и улучшать базу знаний."
                ),
            },
            {
                "icon": "🌐",
                "title": "Многоканальность",
                "description": (
                    "Один FAQ-бот отвечает в нескольких группах и личных диалогах "
                    "одновременно. Настройте разные наборы вопросов для разных "
                    "аудиторий или используйте единую базу для всех."
                ),
            },
        ],
    )


@router.get("/leads", response_class=HTMLResponse)
async def leads_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "leads",
        title="Лид-квалификация",
        icon="🎯",
        description=(
            "Автоматически собирайте и квалифицируйте потенциальных клиентов прямо "
            "в MAX: бот проводит опрос, определяет «горячесть» лида и передаёт его в CRM."
        ),
        features=[
            {
                "icon": "📋",
                "title": "Конструктор квалификационных анкет",
                "description": (
                    "Создайте воронку из вопросов: бюджет, потребность, сроки, "
                    "должность. Каждый ответ сохраняется в профиле лида. "
                    "Условная логика: следующий вопрос зависит от предыдущего ответа."
                ),
            },
            {
                "icon": "⚡",
                "title": "Автоматический скоринг",
                "description": (
                    "Каждому ответу присваивается балл — итоговый скоринг определяет "
                    "«температуру» лида: холодный, тёплый, горячий. Продавцы "
                    "фокусируются на самых перспективных контактах."
                ),
            },
            {
                "icon": "🔀",
                "title": "Маршрутизация по сегментам",
                "description": (
                    "В зависимости от квалификации лид попадает в нужный сценарий: "
                    "горячий → немедленно уведомляет менеджера, тёплый → "
                    "получает nurturing-контент, холодный → добавляется в ретаргетинг."
                ),
            },
            {
                "icon": "🤝",
                "title": "Запись на встречу / звонок",
                "description": (
                    "После квалификации бот предлагает выбрать удобное время "
                    "для звонка или демо из слотов менеджера. Интеграция с "
                    "Calendly или Google Calendar — встреча создаётся автоматически."
                ),
            },
            {
                "icon": "📨",
                "title": "Уведомление команды продаж",
                "description": (
                    "Горячий лид приходит менеджеру в MAX-сообщении со всеми "
                    "данными анкеты, скорингом и ссылкой на профиль в CRM. "
                    "Время реакции сокращается с часов до минут."
                ),
            },
            {
                "icon": "📊",
                "title": "Воронка лидов и конверсия",
                "description": (
                    "Дашборд показывает: сколько лидов начали анкету, сколько "
                    "завершили, распределение по сегментам, конверсию в сделку. "
                    "A/B-тестирование вопросов для оптимизации воронки."
                ),
            },
        ],
    )


@router.get("/assistant", response_class=HTMLResponse)
async def assistant_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "assistant",
        title="AI-ассистент",
        icon="🤖",
        description=(
            "Обученный на ваших материалах AI-ассистент отвечает на вопросы "
            "подписчиков в личных сообщениях 24/7 — как персональный эксперт по вашей теме."
        ),
        features=[
            {
                "icon": "📖",
                "title": "Обучение на собственных данных",
                "description": (
                    "Загрузите архив постов канала, документы, статьи, PDF — ассистент "
                    "проиндексирует всё это и будет отвечать, опираясь именно на ваши "
                    "материалы, а не на общие знания. Ответы всегда в теме."
                ),
            },
            {
                "icon": "💬",
                "title": "Полноценный диалог",
                "description": (
                    "Ассистент помнит контекст разговора: пользователь может "
                    "уточнять, переформулировать, задавать уточняющие вопросы. "
                    "Разговор ощущается естественным, а не как поиск по базе."
                ),
            },
            {
                "icon": "🎭",
                "title": "Настраиваемая личность",
                "description": (
                    "Задайте имя, тон общения (официальный, дружелюбный, экспертный), "
                    "запрещённые темы и приоритетные сценарии. Ассистент будет "
                    "последовательным лицом вашего бренда."
                ),
            },
            {
                "icon": "🔗",
                "title": "Ссылки на источники",
                "description": (
                    "Каждый ответ сопровождается ссылкой на пост или документ, "
                    "из которого взята информация. Пользователь может перейти "
                    "и изучить тему подробнее — это повышает доверие к ассистенту."
                ),
            },
            {
                "icon": "🚨",
                "title": "Передача сложных случаев",
                "description": (
                    "Если ассистент не уверен в ответе или пользователь неудовлетворён, "
                    "диалог передаётся живому оператору в Helpdesk. "
                    "Переход происходит плавно — с сохранением всей истории чата."
                ),
            },
            {
                "icon": "🔄",
                "title": "Актуализация знаний",
                "description": (
                    "При публикации новых постов в канале они автоматически попадают "
                    "в базу знаний ассистента. Не нужно вручную обновлять данные — "
                    "ассистент всегда в курсе последних материалов."
                ),
            },
        ],
    )


@router.get("/helpdesk", response_class=HTMLResponse)
async def helpdesk_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "helpdesk",
        title="Helpdesk",
        icon="🎧",
        description=(
            "Полноценная система поддержки клиентов внутри MAX: принимайте обращения, "
            "распределяйте между операторами и ведите историю переписки в одном месте."
        ),
        features=[
            {
                "icon": "🎫",
                "title": "Тикет-система",
                "description": (
                    "Каждое обращение автоматически получает номер тикета и статус. "
                    "Пользователь видит, что его вопрос принят и обрабатывается — "
                    "это снижает повторные обращения и тревогу ожидания."
                ),
            },
            {
                "icon": "👥",
                "title": "Распределение между операторами",
                "description": (
                    "Автоматическое назначение по очереди, специализации или нагрузке. "
                    "Оператор получает уведомление с полным контекстом обращения. "
                    "Ручная переназначение, если нужен другой специалист."
                ),
            },
            {
                "icon": "💻",
                "title": "Единый интерфейс для команды",
                "description": (
                    "Все тикеты в одном веб-интерфейсе: оператор отвечает здесь, "
                    "сообщение уходит пользователю в MAX. Не нужно переключаться "
                    "между вкладками — вся работа в одном месте."
                ),
            },
            {
                "icon": "⏰",
                "title": "SLA и эскалация по времени",
                "description": (
                    "Задайте время ответа для каждого приоритета: критический — 1 час, "
                    "обычный — 24 часа. Просроченные тикеты автоматически эскалируются "
                    "к старшему оператору или руководителю."
                ),
            },
            {
                "icon": "📝",
                "title": "Шаблоны ответов",
                "description": (
                    "Библиотека готовых ответов на типичные обращения: оператор "
                    "вставляет шаблон одним кликом и при необходимости дополняет его. "
                    "Экономит время и обеспечивает единый стиль коммуникации."
                ),
            },
            {
                "icon": "📊",
                "title": "Метрики качества поддержки",
                "description": (
                    "Среднее время первого ответа, время до закрытия тикета, "
                    "CSAT-оценки от пользователей, нагрузка по операторам. "
                    "Определяйте узкие места и улучшайте процессы поддержки."
                ),
            },
        ],
    )


# ── Аналитика ─────────────────────────────────────────────────────────────────

@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "stats",
        title="Статистика канала",
        icon="📈",
        description=(
            "Детальная аналитика по вашим MAX-каналам: просмотры, охват, рост аудитории "
            "и вовлечённость — всё в одном дашборде с историей и трендами."
        ),
        features=[
            {
                "icon": "👁️",
                "title": "Просмотры и охват постов",
                "description": (
                    "Для каждого поста видите число уникальных просмотров, охват "
                    "и динамику набора: как быстро пост набирает просмотры, "
                    "в какой момент рост замедляется. Сравнение с предыдущими постами."
                ),
            },
            {
                "icon": "📉",
                "title": "Динамика подписчиков",
                "description": (
                    "График роста аудитории с разбивкой: новые подписчики, "
                    "отписки, чистый прирост за любой период. Видите пики и спады, "
                    "соотносите их с конкретными публикациями или событиями."
                ),
            },
            {
                "icon": "⏰",
                "title": "Лучшее время для публикации",
                "description": (
                    "Тепловая карта активности аудитории по часам и дням недели: "
                    "в какое время ваши посты набирают больше всего просмотров. "
                    "Рекомендации оптимального расписания конкретно для вашего канала."
                ),
            },
            {
                "icon": "🏆",
                "title": "Топ-посты",
                "description": (
                    "Рейтинг лучших публикаций по просмотрам, реакциям, "
                    "числу комментариев, кликам по кнопкам. Анализируйте, "
                    "какие темы и форматы работают лучше всего."
                ),
            },
            {
                "icon": "📊",
                "title": "Сравнение периодов",
                "description": (
                    "Сравните текущий месяц с предыдущим или аналогичным периодом "
                    "прошлого года. Видите реальный рост, а не случайные колебания. "
                    "Экспорт данных в CSV для сторонних инструментов."
                ),
            },
            {
                "icon": "🔔",
                "title": "Уведомления об аномалиях",
                "description": (
                    "Получайте алерт, когда пост набирает на 50% больше просмотров "
                    "чем обычно — можно вовремя добавить призыв к действию. "
                    "Или когда резко вырос отток подписчиков — чтобы разобраться в причине."
                ),
            },
        ],
    )


@router.get("/engagement", response_class=HTMLResponse)
async def engagement_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "engagement",
        title="Вовлечённость",
        icon="💬",
        description=(
            "Измеряйте не только просмотры, но и качество взаимодействия аудитории: "
            "реакции, комментарии, пересылки и клики — показатели реального интереса к контенту."
        ),
        features=[
            {
                "icon": "❤️",
                "title": "Анализ реакций",
                "description": (
                    "Видите распределение реакций на каждый пост: не просто общее число, "
                    "но и какие эмодзи ставят чаще всего. Динамика реакций во времени — "
                    "как аудитория реагирует на разные темы и настроения."
                ),
            },
            {
                "icon": "💬",
                "title": "Глубина дискуссий",
                "description": (
                    "Количество комментариев, уникальных участников дискуссии, "
                    "средняя длина обсуждения. Темы, которые вызвали наибольший "
                    "диалог — ориентир для создания контента, провоцирующего разговор."
                ),
            },
            {
                "icon": "🔄",
                "title": "Пересылки и виральность",
                "description": (
                    "Сколько раз пост переслали в другие чаты и каналы — "
                    "главный индикатор виральности. Посты с высоким показателем "
                    "пересылок приносят органических новых подписчиков."
                ),
            },
            {
                "icon": "🖱️",
                "title": "Клики по inline-кнопкам",
                "description": (
                    "CTR каждой кнопки в постах: сколько людей увидело пост "
                    "и сколько кликнуло на ссылку или кнопку. Оптимизируйте "
                    "призывы к действию опираясь на реальные данные."
                ),
            },
            {
                "icon": "📏",
                "title": "Индекс вовлечённости (ER)",
                "description": (
                    "Комплексный показатель ER = (реакции + комментарии + пересылки) "
                    "/ охват × 100%. Бенчмарк по тематике канала, динамика ER "
                    "за выбранный период — рост ER важнее роста числа подписчиков."
                ),
            },
            {
                "icon": "🎯",
                "title": "Сегментация контента",
                "description": (
                    "Автоматическая кластеризация постов по тегам или рубрикам "
                    "с агрегированными метриками вовлечённости по каждому типу. "
                    "Поймите, какие рубрики «работают» лучше остальных."
                ),
            },
        ],
    )


@router.get("/audience-activity", response_class=HTMLResponse)
async def audience_activity_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "audience_activity",
        title="Активность аудитории",
        icon="👥",
        description=(
            "Понимайте поведение подписчиков: когда они онлайн, как давно подписались, "
            "кто самые активные участники, какова реальная «живость» вашей аудитории."
        ),
        features=[
            {
                "icon": "🕐",
                "title": "Онлайн-активность по часам",
                "description": (
                    "Когда ваша аудитория чаще всего онлайн в MAX: почасовой "
                    "и понедельный профиль активности. Планируйте публикации "
                    "на пиковое время для максимального первоначального охвата."
                ),
            },
            {
                "icon": "🏃",
                "title": "Сегменты по активности",
                "description": (
                    "Автоматическое разделение аудитории: «суперактивные» (реагируют "
                    "на каждый пост), «регулярные», «пассивные», «уснувшие» "
                    "(не взаимодействовали более 30 дней). База для реактивации."
                ),
            },
            {
                "icon": "📅",
                "title": "Когорты подписчиков",
                "description": (
                    "Анализ по когортам: подписчики, пришедшие в январе, "
                    "ведут себя иначе, чем пришедшие в марте? Сравнивайте "
                    "вовлечённость разных когорт — находите лучшие источники качественной аудитории."
                ),
            },
            {
                "icon": "🌍",
                "title": "География аудитории",
                "description": (
                    "Разбивка подписчиков по странам и регионам, если API "
                    "предоставляет эти данные. Помогает при планировании времени "
                    "публикаций и языка контента для мультирегиональных каналов."
                ),
            },
            {
                "icon": "👑",
                "title": "Топ-участники",
                "description": (
                    "Рейтинг самых активных читателей: чаще всего ставят реакции, "
                    "пишут комментарии, пересылают посты. Программа лояльности "
                    "для суперфанов, которые продвигают ваш канал."
                ),
            },
            {
                "icon": "⚠️",
                "title": "Отток и его причины",
                "description": (
                    "После каких постов был пик отписок? Корреляция между типами "
                    "контента и оттоком подписчиков. Раннее предупреждение: "
                    "если отток резко вырос — немедленное уведомление."
                ),
            },
        ],
    )


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "reports",
        title="Отчёты",
        icon="📋",
        description=(
            "Автоматические сводные отчёты по каналам и ботам: еженедельные дайджесты, "
            "кастомные выгрузки и белые отчёты для клиентов или руководства."
        ),
        features=[
            {
                "icon": "📬",
                "title": "Автоматические периодические отчёты",
                "description": (
                    "Настройте отправку еженедельного или ежемесячного отчёта "
                    "на email или в MAX-бота. Никаких ручных действий: "
                    "каждый понедельник утром готовая сводка за прошлую неделю."
                ),
            },
            {
                "icon": "🎨",
                "title": "Кастомный конструктор отчётов",
                "description": (
                    "Выберите нужные метрики, каналы, периоды и порядок блоков. "
                    "Сохраните шаблон и переиспользуйте его. Поддержка сравнительных "
                    "таблиц, графиков и сводных показателей."
                ),
            },
            {
                "icon": "📄",
                "title": "White-label отчёты для клиентов",
                "description": (
                    "Замените логотип MaxBot на ваш собственный, настройте цвета "
                    "и добавьте контактные данные. Загружайте PDF-отчёты с вашим "
                    "брендингом и отправляйте клиентам напрямую."
                ),
            },
            {
                "icon": "📊",
                "title": "Сравнительные отчёты по каналам",
                "description": (
                    "Если управляете несколькими каналами — сводный отчёт "
                    "сравнивает их между собой: кто растёт быстрее, у кого "
                    "лучше ER, где больше всего проблем. Единая картина по портфелю."
                ),
            },
            {
                "icon": "⬇️",
                "title": "Экспорт в PDF, Excel, CSV",
                "description": (
                    "Любой отчёт скачивается в нужном формате: PDF для презентаций, "
                    "Excel для дальнейшего анализа, CSV для загрузки в BI-системы. "
                    "API-доступ к данным для интеграции с вашими инструментами."
                ),
            },
            {
                "icon": "🔗",
                "title": "Публичные ссылки на отчёты",
                "description": (
                    "Поделитесь ссылкой на живой отчёт с коллегой или клиентом — "
                    "они откроют актуальные данные в браузере без регистрации. "
                    "Настраиваемый срок действия ссылки и защита паролем."
                ),
            },
        ],
    )


# ── Монетизация ───────────────────────────────────────────────────────────────

@router.get("/paid-access", response_class=HTMLResponse)
async def paid_access_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "paid_access",
        title="Платный доступ",
        icon="🔐",
        description=(
            "Монетизируйте эксклюзивный контент: создавайте закрытые каналы или группы "
            "с платным доступом — бот автоматически управляет подписками и оплатами."
        ),
        features=[
            {
                "icon": "💳",
                "title": "Платёжная интеграция",
                "description": (
                    "Приём оплаты через ЮKassa, Stripe, CloudPayments и другие шлюзы. "
                    "Пользователь оплачивает прямо в MAX — без перехода на сайт. "
                    "Автоматическое выставление чеков согласно 54-ФЗ."
                ),
            },
            {
                "icon": "📅",
                "title": "Гибкие тарифы и пробный период",
                "description": (
                    "Месячная, квартальная, годовая подписка или разовый доступ. "
                    "Бесплатный пробный период с автоматическим переходом на платный. "
                    "Скидка за длительную подписку настраивается в несколько кликов."
                ),
            },
            {
                "icon": "🚪",
                "title": "Автоматический контроль доступа",
                "description": (
                    "Оплатил → бот добавляет в закрытый канал. "
                    "Подписка истекла или не продлена → автоматическое исключение. "
                    "Никаких ручных действий — система работает 24/7."
                ),
            },
            {
                "icon": "🎁",
                "title": "Промокоды и реферальные скидки",
                "description": (
                    "Создавайте промокоды на скидку или бесплатный период: "
                    "для партнёров, подарков, акций. Реферальная программа: "
                    "текущий подписчик получает бонус за каждого приведённого друга."
                ),
            },
            {
                "icon": "🔔",
                "title": "Напоминания о продлении",
                "description": (
                    "За 7 и 1 день до истечения подписки бот напоминает "
                    "пользователю и присылает кнопку «Продлить». "
                    "Автоплатёж для привязанных карт — снижает отток."
                ),
            },
            {
                "icon": "📊",
                "title": "Финансовая аналитика",
                "description": (
                    "MRR (ежемесячная выручка), количество активных подписчиков, "
                    "churn rate, LTV. Видите рост дохода в динамике и понимаете, "
                    "какие тарифы приносят больше всего выручки."
                ),
            },
        ],
    )


@router.get("/products", response_class=HTMLResponse)
async def products_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "products",
        title="Цифровые товары",
        icon="📦",
        description=(
            "Продавайте PDF, курсы, шаблоны, чеклисты и другие цифровые продукты "
            "напрямую через MAX-бота — покупатель получает файл сразу после оплаты."
        ),
        features=[
            {
                "icon": "🗂️",
                "title": "Каталог товаров",
                "description": (
                    "Создавайте карточки товаров с описанием, изображением, "
                    "ценой и превью. Покупатель листает каталог прямо в MAX, "
                    "как в интернет-магазине, и нажимает «Купить» не выходя из приложения."
                ),
            },
            {
                "icon": "⚡",
                "title": "Мгновенная доставка файлов",
                "description": (
                    "После оплаты бот немедленно присылает ссылку для скачивания "
                    "или сам файл в личное сообщение. Никаких ожиданий "
                    "и ручной отправки — продажи работают пока вы спите."
                ),
            },
            {
                "icon": "🔒",
                "title": "Защита от пиратства",
                "description": (
                    "Ссылки на скачивание одноразовые или с коротким сроком жизни. "
                    "Для PDF — водяной знак с именем покупателя. "
                    "Лог скачиваний: знаете, кто и когда получил файл."
                ),
            },
            {
                "icon": "🎁",
                "title": "Бандлы и апсейл",
                "description": (
                    "Создавайте наборы (3 курса по цене 2), предлагайте апсейл "
                    "сразу после покупки: «Покупатели этого также берут...». "
                    "Увеличивайте средний чек без дополнительного трафика."
                ),
            },
            {
                "icon": "🔁",
                "title": "Доступ к обновлениям",
                "description": (
                    "При выходе новой версии материала все покупатели автоматически "
                    "получают уведомление в MAX с обновлённой ссылкой. "
                    "Это повышает ценность продукта и лояльность аудитории."
                ),
            },
            {
                "icon": "📈",
                "title": "Аналитика продаж",
                "description": (
                    "Выручка по каждому товару, конверсия просмотра в покупку, "
                    "источники трафика, повторные покупки. Понимайте, "
                    "какие продукты продвигать активнее, а какие — переработать."
                ),
            },
        ],
    )


@router.get("/affiliate", response_class=HTMLResponse)
async def affiliate_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "affiliate",
        title="Партнёрские ссылки",
        icon="🤝",
        description=(
            "Создайте реферальную программу для вашего канала: подписчики рекомендуют "
            "вас и получают вознаграждение, а вы получаете новых клиентов по минимальной стоимости."
        ),
        features=[
            {
                "icon": "🔗",
                "title": "Генерация уникальных ссылок",
                "description": (
                    "Каждый участник получает персональную реферальную ссылку "
                    "через бота одной командой. Ссылка отслеживает переходы, "
                    "регистрации и покупки, привязанные к этому рефереру."
                ),
            },
            {
                "icon": "💰",
                "title": "Гибкая система вознаграждений",
                "description": (
                    "Выплачивайте фиксированную сумму за каждого приведённого "
                    "подписчика или процент от его первой покупки. "
                    "Многоуровневая реферальная цепочка для партнёрских сетей."
                ),
            },
            {
                "icon": "📊",
                "title": "Личный кабинет реферера",
                "description": (
                    "В ответ на команду /stats бот показывает: сколько переходов, "
                    "сколько зарегистрировалось, текущий баланс вознаграждений "
                    "и история выплат — всё прямо в чате."
                ),
            },
            {
                "icon": "🏆",
                "title": "Рейтинг топ-партнёров",
                "description": (
                    "Публичный или приватный рейтинг лучших рефереров: стимулирует "
                    "конкуренцию и подчёркивает ценность активных партнёров. "
                    "Бейджи и специальный статус для топ-10."
                ),
            },
            {
                "icon": "📤",
                "title": "Выплаты и вывод средств",
                "description": (
                    "Накопленное вознаграждение выводится по заявке на карту, "
                    "электронный кошелёк или в виде промокода на ваши же продукты. "
                    "Минимальная сумма вывода задаётся администратором."
                ),
            },
            {
                "icon": "🛡️",
                "title": "Антифрод защита",
                "description": (
                    "Система автоматически детектирует подозрительную активность: "
                    "саморефералы, накрутки переходов с одного IP, "
                    "аккаунты-боты. Подозрительные заявки блокируются до проверки."
                ),
            },
        ],
    )


@router.get("/ads", response_class=HTMLResponse)
async def ads_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "ads",
        title="Реклама",
        icon="📣",
        description=(
            "Управляйте рекламными интеграциями в вашем канале: автоматизируйте "
            "размещение, отслеживайте эффективность и получайте оплату без посредников."
        ),
        features=[
            {
                "icon": "📅",
                "title": "Биржа рекламных размещений",
                "description": (
                    "Рекламодатели бронируют слоты в вашем контент-плане напрямую: "
                    "выбирают дату, формат, согласовывают материал и оплачивают. "
                    "Вы подтверждаете — бот публикует в нужное время автоматически."
                ),
            },
            {
                "icon": "💼",
                "title": "Управление рекламным контентом",
                "description": (
                    "Медиакит канала с актуальной статистикой генерируется автоматически "
                    "и всегда доступен по ссылке для рекламодателей. "
                    "Разграничение редакционного и рекламного контента в расписании."
                ),
            },
            {
                "icon": "🔖",
                "title": "Автоматическая маркировка рекламы",
                "description": (
                    "Рекламные посты автоматически помечаются тегом «Реклама» "
                    "и содержат ERID-токен согласно законодательству о маркировке "
                    "интернет-рекламы — без лишних усилий с вашей стороны."
                ),
            },
            {
                "icon": "📊",
                "title": "Статистика для рекламодателя",
                "description": (
                    "После завершения размещения рекламодатель получает "
                    "автоматический отчёт: просмотры, клики, охват, "
                    "реакции на пост — прозрачность повышает доверие и повторные заказы."
                ),
            },
            {
                "icon": "🔄",
                "title": "Контроль частоты рекламы",
                "description": (
                    "Задайте максимальное число рекламных постов в день/неделю "
                    "и минимальный интервал между ними. Система не позволит "
                    "перегрузить аудиторию рекламой даже при высоком спросе."
                ),
            },
            {
                "icon": "💳",
                "title": "Приём оплаты и акты",
                "description": (
                    "Рекламодатель оплачивает размещение картой или по счёту — "
                    "все документы (договор, акт) формируются автоматически. "
                    "Деньги поступают на ваш счёт сразу после публикации."
                ),
            },
        ],
    )


# ── Интеграции ────────────────────────────────────────────────────────────────

@router.get("/webhooks", response_class=HTMLResponse)
async def webhooks_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "webhooks",
        title="Исходящие Webhook",
        icon="🔗",
        description=(
            "Отправляйте события из MAX (новые подписчики, реакции, покупки) "
            "в любые внешние системы в режиме реального времени через HTTP webhook."
        ),
        features=[
            {
                "icon": "⚡",
                "title": "Широкий набор триггеров",
                "description": (
                    "Настройте отправку webhook на любое событие: новый подписчик, "
                    "отписка, новый комментарий, покупка товара, новый лид, "
                    "публикация поста ботом, ошибка планировщика."
                ),
            },
            {
                "icon": "🔧",
                "title": "Кастомный payload",
                "description": (
                    "Задайте структуру JSON-тела запроса с помощью шаблона: "
                    "включайте только нужные поля, переименовывайте их "
                    "под формат принимающей системы без промежуточного сервиса."
                ),
            },
            {
                "icon": "🔒",
                "title": "Аутентификация запросов",
                "description": (
                    "Подпись каждого запроса HMAC-SHA256-секретом — принимающая "
                    "сторона может проверить подлинность. Поддержка Bearer-токена "
                    "в заголовках для базовой HTTP-аутентификации."
                ),
            },
            {
                "icon": "🔁",
                "title": "Повторные попытки и очередь",
                "description": (
                    "Если сервер не ответил — система автоматически повторит запрос "
                    "3 раза с экспоненциальной задержкой. Все события попадают "
                    "в очередь и не теряются при временной недоступности получателя."
                ),
            },
            {
                "icon": "📋",
                "title": "Журнал вызовов",
                "description": (
                    "История всех исходящих webhook с кодом ответа, временем "
                    "выполнения и телом запроса/ответа. Быстрая отладка "
                    "интеграции прямо из интерфейса без посторонних инструментов."
                ),
            },
            {
                "icon": "🧪",
                "title": "Тестирование без реального события",
                "description": (
                    "Отправьте тестовый webhook с произвольным payload одним кликом — "
                    "убедитесь, что принимающий сервис корректно обрабатывает "
                    "данные, ещё до первого реального события."
                ),
            },
        ],
    )


@router.get("/crm", response_class=HTMLResponse)
async def crm_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "crm",
        title="CRM",
        icon="👔",
        description=(
            "Синхронизируйте подписчиков и лиды из MAX с вашей CRM-системой: "
            "amoCRM, Bitrix24, HubSpot и другими — данные всегда актуальны в обеих системах."
        ),
        features=[
            {
                "icon": "🔄",
                "title": "Двусторонняя синхронизация",
                "description": (
                    "Новый лид из MAX появляется в CRM как сделка или контакт. "
                    "Обновление статуса сделки в CRM — бот отправляет подписчику "
                    "соответствующее сообщение в MAX. Данные консистентны в обеих системах."
                ),
            },
            {
                "icon": "🗺️",
                "title": "Маппинг полей",
                "description": (
                    "Гибкое соответствие полей MAX и CRM: ответы на вопросы "
                    "квалификационной анкеты → кастомные поля сделки. "
                    "Визуальный редактор маппинга без написания кода."
                ),
            },
            {
                "icon": "🏢",
                "title": "Готовые коннекторы",
                "description": (
                    "Нативные интеграции с amoCRM и Bitrix24, "
                    "универсальный коннектор через REST API для любой другой системы. "
                    "Настройка по шагам занимает менее 10 минут."
                ),
            },
            {
                "icon": "🎯",
                "title": "Автоматические воронки",
                "description": (
                    "Движение по этапам воронки CRM автоматически запускает "
                    "действия в MAX: отправить сообщение, добавить в канал, "
                    "запустить квалификационный опрос на новом этапе."
                ),
            },
            {
                "icon": "📊",
                "title": "Сквозная аналитика",
                "description": (
                    "Связывайте источники трафика MAX с результатами в CRM: "
                    "какой пост, кнопка или рассылка принесла больше всего "
                    "закрытых сделок. ROMI по каждому каналу привлечения."
                ),
            },
            {
                "icon": "📝",
                "title": "История коммуникаций в CRM",
                "description": (
                    "Вся переписка с клиентом в MAX автоматически логируется "
                    "в карточку контакта CRM. Менеджер видит полный контекст "
                    "общения до того, как берёт трубку."
                ),
            },
        ],
    )


@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "notifications",
        title="Входящие уведомления",
        icon="📥",
        description=(
            "Принимайте данные от внешних систем и транслируйте их в MAX-каналы или личные "
            "сообщения: мониторинг серверов, уведомления из CRM, триггеры от сайта."
        ),
        features=[
            {
                "icon": "🌐",
                "title": "Универсальный HTTP endpoint",
                "description": (
                    "Для каждого бота генерируется уникальный URL. "
                    "Любая внешняя система делает POST-запрос с данными — "
                    "бот форматирует и отправляет сообщение в нужный чат или канал."
                ),
            },
            {
                "icon": "🎨",
                "title": "Шаблоны оформления уведомлений",
                "description": (
                    "Настройте, как выглядит уведомление в MAX: какие поля JSON "
                    "вставлять в текст, какой эмодзи-префикс использовать, "
                    "добавлять ли inline-кнопки с быстрыми действиями."
                ),
            },
            {
                "icon": "🔀",
                "title": "Маршрутизация по типу события",
                "description": (
                    "Разные события → в разные чаты. Ошибка деплоя → в чат DevOps, "
                    "новая заявка → в чат отдела продаж, платёж → в финансовый канал. "
                    "Правила маршрутизации по полям JSON."
                ),
            },
            {
                "icon": "⚡",
                "title": "Готовые интеграции",
                "description": (
                    "Преднастроенные шаблоны для популярных сервисов: Grafana, "
                    "GitHub Actions, Sentry, Jira, Stripe webhook, Google Forms. "
                    "Вставьте URL, включите — готово к работе за 2 минуты."
                ),
            },
            {
                "icon": "🔍",
                "title": "Фильтрация и дедупликация",
                "description": (
                    "Задайте условия, при которых уведомление отправляется: "
                    "только если severity == 'critical', только рабочие часы, "
                    "не чаще одного раза в 10 минут при повторяющихся событиях."
                ),
            },
            {
                "icon": "📋",
                "title": "Журнал входящих запросов",
                "description": (
                    "История всех входящих событий с временной меткой, "
                    "источником, содержимым и статусом отправки в MAX. "
                    "Повторная отправка зависшего уведомления одним кликом."
                ),
            },
        ],
    )


@router.get("/whitelabel", response_class=HTMLResponse)
async def whitelabel_page(request: Request, session: DBSession):
    user = await _require_user(request, session)
    return _coming_soon(request, user, "whitelabel",
        title="White-label",
        icon="🏷️",
        description=(
            "Перепродавайте MaxBot под собственным брендом: ваш логотип, ваш домен, "
            "ваши цены — клиенты видят ваш продукт, а не MaxBot."
        ),
        features=[
            {
                "icon": "🎨",
                "title": "Полный ребрендинг интерфейса",
                "description": (
                    "Загрузите логотип, задайте цветовую схему, название продукта "
                    "и фавикон. Клиенты работают в интерфейсе с вашим брендом — "
                    "ни одного упоминания MaxBot в UI."
                ),
            },
            {
                "icon": "🌐",
                "title": "Кастомный домен",
                "description": (
                    "Разверните панель управления на своём домене или поддомене: "
                    "app.yourcompany.ru. SSL-сертификат выпускается автоматически. "
                    "Клиенты заходят на ваш адрес, а не на maxbot.ru."
                ),
            },
            {
                "icon": "👥",
                "title": "Управление клиентами",
                "description": (
                    "Создавайте аккаунты для ваших клиентов, задавайте лимиты "
                    "на количество ботов и каналов, включайте/отключайте функции "
                    "по тарифу. Полный контроль над тем, что видит каждый клиент."
                ),
            },
            {
                "icon": "💳",
                "title": "Собственный биллинг",
                "description": (
                    "Установите свои цены и тарифные планы. Принимайте оплату "
                    "от клиентов через свой платёжный шлюз. Вы платите MaxBot "
                    "оптовую ставку — маржа целиком ваша."
                ),
            },
            {
                "icon": "📧",
                "title": "Брендированные письма и уведомления",
                "description": (
                    "Все системные письма (регистрация, восстановление пароля, "
                    "отчёты) отправляются с вашего домена и с вашим логотипом. "
                    "Никаких «от MaxBot» — полная иллюзия собственного продукта."
                ),
            },
            {
                "icon": "🛠️",
                "title": "Выделенная поддержка партнёров",
                "description": (
                    "White-label партнёры получают приоритетный доступ к технической "
                    "поддержке, ранний доступ к новым функциям и персонального "
                    "менеджера для помощи с онбордингом клиентов."
                ),
            },
        ],
    )
