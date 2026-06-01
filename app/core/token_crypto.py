"""
Symmetric Fernet encryption for OAuth tokens stored in DB.

OAUTH_TOKEN_FERNET_KEY (env, base64) — required in prod. Generate with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Fernet ciphertext always starts with 'gAAAAA' (base64 of version byte 0x80).
We use that as a cheap detector for legacy plaintext rows during migration.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENV_KEY = "OAUTH_TOKEN_FERNET_KEY"
_FERNET_PREFIX = "gAAAAA"


class TokenCryptoError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = os.environ.get(ENV_KEY)
    if not key:
        raise TokenCryptoError(
            f"{ENV_KEY} not set. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        raise TokenCryptoError(f"{ENV_KEY} is not a valid Fernet key: {e}")


def is_encrypted(value: Optional[str]) -> bool:
    return bool(value) and value.startswith(_FERNET_PREFIX)


def encrypt_token(plain: Optional[str]) -> Optional[str]:
    if plain is None or plain == "":
        return plain
    if is_encrypted(plain):
        return plain
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_token(stored: Optional[str]) -> Optional[str]:
    if stored is None or stored == "":
        return stored
    if not is_encrypted(stored):
        return stored
    try:
        return _fernet().decrypt(stored.encode()).decode()
    except InvalidToken:
        logger.error("decrypt_token: InvalidToken — key rotated or DB corrupted")
        raise TokenCryptoError("invalid Fernet token (key mismatch?)")
