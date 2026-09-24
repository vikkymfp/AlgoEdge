from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class CredentialEncryptionUnavailable(RuntimeError):
    """Raised when encrypt/decrypt is attempted with no encryption key
    configured (ALGOEDGE_CREDENTIAL_ENCRYPTION_KEY is blank)."""


def is_configured(encryption_key: str) -> bool:
    return bool(encryption_key)


def encrypt(value: str, encryption_key: str) -> bytes:
    if not encryption_key:
        raise CredentialEncryptionUnavailable(
            "ALGOEDGE_CREDENTIAL_ENCRYPTION_KEY is not set - cannot encrypt for storage."
        )
    fernet = Fernet(encryption_key.encode())
    return fernet.encrypt(value.encode())


def decrypt(token: bytes, encryption_key: str) -> str:
    if not encryption_key:
        raise CredentialEncryptionUnavailable(
            "ALGOEDGE_CREDENTIAL_ENCRYPTION_KEY is not set - cannot decrypt stored credentials."
        )
    fernet = Fernet(encryption_key.encode())
    try:
        return fernet.decrypt(token).decode()
    except InvalidToken as error:
        raise CredentialEncryptionUnavailable(
            "Stored credential could not be decrypted - the encryption key may have changed."
        ) from error


def mask_secret(value: str | None) -> str:
    """A fixed-length dot mask for secrets/tokens - deliberately reveals
    nothing about length or content, unlike a partial reveal."""
    if not value:
        return ""
    return "•" * 16


def mask_key(value: str | None, keep_prefix: int = 2, keep_suffix: int = 4) -> str:
    """Partial reveal for the (less sensitive, more of an identifier) API
    key only - e.g. "gw...ABCD" - matching the spec's own display example.
    Falls back to a full mask if the value is too short to partially reveal
    safely."""
    if not value:
        return ""
    if len(value) <= keep_prefix + keep_suffix:
        return mask_secret(value)
    return f"{value[:keep_prefix]}{'.' * 3}{value[-keep_suffix:]}"


def reference_hint(value: str | None) -> str | None:
    """A short, non-reversible-enough-to-matter reference for audit logs -
    last 4 characters only, never the raw value. Used so audit history can
    show "...ABCD was updated" without ever storing/logging the secret."""
    if not value:
        return None
    return value[-4:]
