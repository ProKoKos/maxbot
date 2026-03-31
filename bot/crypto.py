"""
Token encryption/decryption using Fernet (AES-128-CBC + HMAC-SHA256).

Key generation (run once, paste into .env):
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

The key must be 32 URL-safe base64-encoded bytes (44 chars).
"""
from cryptography.fernet import Fernet, InvalidToken

from shared.config import get_settings

settings = get_settings()


def _fernet() -> Fernet:
    key = settings.encryption_key.encode()
    return Fernet(key)


def encrypt_token(plain: str) -> str:
    """Encrypt a plaintext bot token for storage in DB."""
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    """Decrypt a stored bot token. Raises ValueError on bad key/corrupted data."""
    try:
        return _fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Cannot decrypt bot token — wrong ENCRYPTION_KEY or corrupted data") from exc
