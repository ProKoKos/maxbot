"""
Inline keyboard builders for Max Bot API.

Max inline_keyboard attachment structure:
{
  "type": "inline_keyboard",
  "payload": {
    "buttons": [
      [  <- row
        {"type": "link", "text": "Label", "url": "https://..."}
      ]
    ]
  }
}
"""


def comment_button(group_link: str, group_message_id: str | None = None) -> dict:
    """
    Returns an inline_keyboard attachment with a single 'Прокомментировать' button.

    If group_message_id is provided and the group supports message links,
    the button deep-links directly to that message.
    Otherwise links to the group itself.
    """
    # Max doesn't have a universal message permalink format documented.
    # We use the group link and append message fragment when available.
    url = group_link
    if group_message_id:
        # Attempt deep link — works when group has a public username/link
        url = f"{group_link.rstrip('/')}?mid={group_message_id}"

    return {
        "type": "inline_keyboard",
        "payload": {
            "buttons": [
                [
                    {
                        "type": "link",
                        "text": "💬 Прокомментировать",
                        "url": url,
                    }
                ]
            ]
        },
    }


def discussion_header(channel_name: str, channel_post_id: str, channel_link: str = "") -> str:
    """
    Text prefix prepended to duplicated posts in the discussion group.
    If channel_link is provided, the channel name becomes a clickable link.
    """
    if channel_link:
        name_part = f"[{channel_name}]({channel_link})"
    else:
        name_part = channel_name
    return f"📢 *Пост из канала {name_part}*\n\n"
