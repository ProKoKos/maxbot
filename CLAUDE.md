# MaxBot — CLAUDE.md

Общение, рассуждения, коммиты, комментарии — **на русском**.

## Сервер

```bash
ssh wmoc-prod  # prokos@213.110.208.173:4795, проект ~/maxbot
```

## Деплой

```bash
git add <файлы> && git commit -m "..." && git push origin main
ssh wmoc-prod "cd ~/maxbot && git pull && docker compose build --no-cache && docker compose up -d"
```

## Важно

- MAX API не поддерживает mute/restrict участников — вместо этого удаляем их сообщения (`handlers.py:_delete_if_unverified`)
- При новой миграции пересобирать **migrate** вместе с остальными (он отдельный образ)
- `WelcomeConfig` имеет приоритет над `ChannelGroupPair` при верификации
- `group_link` fallback: MAX API → `ChannelGroupPair.group_link` → ручной ввод
- Токены ботов шифруются Fernet (`ENCRYPTION_KEY` из `.env`)
- Коммиты без `Co-Authored-By` — не добавлять эту строку
