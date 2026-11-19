"""
Merchant API token signing.

Production auth previously accepted the bare merchant UUID as a Bearer token,
so anyone who knew (or saw) a merchant id could act as that merchant. Tokens
are now `<merchant_uuid>.<hmac_sha256(auth_token_secret, merchant_uuid)>`;
only the holder of AUTH_TOKEN_SECRET can mint a valid token.
"""
import hashlib
import hmac
from uuid import UUID

from app.core.config import settings


class TokenSecretNotConfiguredError(RuntimeError):
    pass


def _signature(merchant_id: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), merchant_id.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_merchant_token(merchant_id: UUID | str, secret: str | None = None) -> str:
    secret = secret if secret is not None else settings.auth_token_secret
    if not secret:
        raise TokenSecretNotConfiguredError("AUTH_TOKEN_SECRET is not configured")
    merchant_str = str(UUID(str(merchant_id)))
    return f"{merchant_str}.{_signature(merchant_str, secret)}"


def verify_merchant_token(token: str, secret: str | None = None) -> UUID | None:
    """Returns the merchant UUID if the token is authentic, otherwise None."""
    secret = secret if secret is not None else settings.auth_token_secret
    if not secret:
        raise TokenSecretNotConfiguredError("AUTH_TOKEN_SECRET is not configured")
    merchant_part, sep, sig = token.partition(".")
    if not sep or not sig:
        return None
    try:
        merchant_uuid = UUID(merchant_part)
    except (ValueError, TypeError):
        return None
    expected = _signature(str(merchant_uuid), secret)
    if not hmac.compare_digest(expected, sig):
        return None
    return merchant_uuid
