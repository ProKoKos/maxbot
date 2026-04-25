"""
Сборщики inline-клавиатуры и заголовков для постов MAX.

Структура inline_keyboard в MAX Bot API::

    {
      "type": "inline_keyboard",
      "payload": {
        "buttons": [
          [  # ряд кнопок
            {"type": "link", "text": "Метка", "url": "https://..."}
          ]
        ]
      }
    }

Здесь же — функция для шапки поста, дублируемого из канала в группу
обсуждений: она помечает источник и опционально превращает имя канала
в кликабельную ссылку.
"""
from bot.constants import COMMENT_BUTTON_LABEL, DISCUSSION_HEADER_TEMPLATE


def comment_button(group_link: str, group_message_id: str | None = None) -> dict:
    """Inline-кнопка «💬 Прокомментировать», ведущая в группу обсуждений.

    Если задан ``group_message_id`` и группа имеет публичную ссылку,
    к URL добавляется ``?mid=<id>`` — попытка прыгнуть сразу к
    обсуждению конкретного поста. У MAX нет универсального формата
    permalink на сообщение, так что это эвристика: если она не сработает,
    кнопка просто откроет группу.
    """
    url = group_link
    if group_message_id:
        # rstrip('/') — чтобы не получить дублированный «/» в URL.
        url = f"{group_link.rstrip('/')}?mid={group_message_id}"

    return {
        "type": "inline_keyboard",
        "payload": {
            "buttons": [
                [
                    {
                        "type": "link",
                        "text": COMMENT_BUTTON_LABEL,
                        "url": url,
                    }
                ]
            ]
        },
    }


def discussion_header(channel_name: str, channel_post_id: str, channel_link: str = "") -> str:
    """Префикс, добавляемый перед текстом поста, продублированного в группу.

    Если есть ``channel_link``, имя канала оборачивается в Markdown-ссылку.
    ``channel_post_id`` сейчас не используется в формате (зарезервирован
    под будущий «прыжок» в конкретный пост канала).
    """
    if channel_link:
        name_part = f"[{channel_name}]({channel_link})"
    else:
        name_part = channel_name
    return DISCUSSION_HEADER_TEMPLATE.format(name=name_part)
