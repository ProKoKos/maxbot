"""
Шифрование/расшифровка чувствительных строк через Fernet
(AES-128-CBC + HMAC-SHA256).

Используется для:
  • токенов ботов MAX (поле ``Bot.encrypted_token``);
  • API-ключей AI-моделей (поле ``AssistantConfig.api_key``).

Генерация ключа (выполнить один раз и положить в ``.env``)::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Ключ — 32 байта, закодированные в URL-safe base64 (44 символа).
ВАЖНО: смена ключа делает все ранее зашифрованные значения
нечитаемыми. Bot/AssistantConfig с битыми токенами supervisor/web
просто пропустят (см. handlers/supervisor).
"""
from cryptography.fernet import Fernet, InvalidToken

from shared.config import get_settings

settings = get_settings()


def _fernet() -> Fernet:
    """Создаёт экземпляр Fernet с ключом из настроек.

    Не кешируем — Fernet дешёвый, а кеш мог бы стать узким местом
    при ротации ключа в будущем.
    """
    key = settings.encryption_key.encode()
    return Fernet(key)


def encrypt_token(plain: str) -> str:
    """Шифрует строку для хранения в БД."""
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    """Расшифровывает строку, выкидывая ValueError на битых данных.

    Заменяем cryptography'шный InvalidToken на ValueError, чтобы
    вызывающему коду не нужно было импортировать cryptography.
    Сообщение даёт подсказку про самую частую причину — смену
    ENCRYPTION_KEY между деплоями.
    """
    try:
        return _fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Cannot decrypt bot token — wrong ENCRYPTION_KEY or corrupted data") from exc
