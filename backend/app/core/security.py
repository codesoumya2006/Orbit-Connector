from cryptography.fernet import Fernet
from app.core.config import settings

def _fernet():
    key = settings.credential_encryption_key
    if not key:
        raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY is not configured. Generate a Fernet key and set it in .env before saving credentials.")
    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY is invalid. Generate a valid Fernet key.") from exc

def encrypt_secret(value: str | None) -> str | None:
    return _fernet().encrypt(value.encode()).decode() if value else None

def decrypt_secret(value: str | None) -> str | None:
    return _fernet().decrypt(value.encode()).decode() if value else None
