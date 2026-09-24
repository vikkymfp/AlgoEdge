import pytest
from cryptography.fernet import Fernet

from algoedge import credential_manager
from algoedge.credential_manager import CredentialEncryptionUnavailable


def test_encrypt_decrypt_round_trip() -> None:
    key = Fernet.generate_key().decode()

    encrypted = credential_manager.encrypt("super-secret-value", key)
    decrypted = credential_manager.decrypt(encrypted, key)

    assert decrypted == "super-secret-value"
    assert b"super-secret-value" not in encrypted


def test_encrypt_without_key_raises() -> None:
    with pytest.raises(CredentialEncryptionUnavailable):
        credential_manager.encrypt("value", "")


def test_decrypt_without_key_raises() -> None:
    with pytest.raises(CredentialEncryptionUnavailable):
        credential_manager.decrypt(b"whatever", "")


def test_decrypt_with_wrong_key_raises_unavailable_not_a_raw_crypto_error() -> None:
    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()
    encrypted = credential_manager.encrypt("value", key_a)

    with pytest.raises(CredentialEncryptionUnavailable):
        credential_manager.decrypt(encrypted, key_b)


def test_mask_secret_never_reveals_content_or_length() -> None:
    short = credential_manager.mask_secret("ab")
    long = credential_manager.mask_secret("a-very-long-access-token-value-indeed")

    assert short == long  # fixed-length mask regardless of input length
    assert "ab" not in short
    assert "a-very-long-access-token-value-indeed" not in long


def test_mask_secret_of_none_or_empty_is_empty() -> None:
    assert credential_manager.mask_secret(None) == ""
    assert credential_manager.mask_secret("") == ""


def test_mask_key_reveals_only_prefix_and_suffix() -> None:
    masked = credential_manager.mask_key("gw_1234567890ABCD")

    assert masked.startswith("gw")
    assert masked.endswith("ABCD")
    assert "1234567890" not in masked


def test_mask_key_too_short_falls_back_to_full_mask() -> None:
    masked = credential_manager.mask_key("short")

    assert "short" not in masked


def test_reference_hint_is_last_four_characters_only() -> None:
    assert credential_manager.reference_hint("gw_1234567890ABCD") == "ABCD"


def test_reference_hint_of_none_is_none() -> None:
    assert credential_manager.reference_hint(None) is None
