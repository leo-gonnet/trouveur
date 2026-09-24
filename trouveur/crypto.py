"""Symmetric encryption for the one secret we store on a user's behalf: their LLM API key.

Decrypted only in the moment a request is built, never logged, and never rendered back to the
browser -- the settings form shows a fingerprint instead.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from trouveur.config import get_settings


class CredentialError(Exception):
    """A stored credential could not be read with the configured encryption key."""


@lru_cache(maxsize=1)
def _cipher() -> Fernet:
    secret = get_settings().encryption_key
    if not secret or secret == "dev-only-insecure-encryption-key":
        raise CredentialError(
            "ENCRYPTION_KEY is unset or still the development default. Set it to a "
            "random secret before storing any user's API key."
        )
    # Fernet wants 32 url-safe base64 bytes; the operator supplies an arbitrary passphrase.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def encrypt(plaintext: str) -> bytes:
    return _cipher().encrypt(plaintext.encode())


def decrypt(token: bytes) -> str:
    try:
        return _cipher().decrypt(bytes(token)).decode()
    except InvalidToken as exc:
        raise CredentialError(
            "A stored API key could not be decrypted. ENCRYPTION_KEY has probably "
            "changed; the affected users must re-enter their key."
        ) from exc


def fingerprint(plaintext: str) -> str:
    """A stable, non-reversible label so the UI can say WHICH key is stored. Not the last four
    characters of the key: that is key material, and it ends up in screenshots."""
    return hashlib.sha256(plaintext.encode()).hexdigest()[:12]
