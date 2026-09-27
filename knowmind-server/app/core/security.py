from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(subject: str, *, token_version: int = 0) -> tuple[str, int]:
    """返回 (jwt, expires_in 秒)。"""
    days = settings.access_token_expire_days
    expire = datetime.now(timezone.utc) + timedelta(days=days)
    exp_ts = int(expire.timestamp())
    payload = {"sub": subject, "exp": exp_ts, "typ": "access", "ver": token_version}
    token = jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return token, int(timedelta(days=days).total_seconds())


def create_refresh_token(subject: str, *, token_version: int = 0) -> tuple[str, int]:
    days = settings.refresh_token_expire_days
    expire = datetime.now(timezone.utc) + timedelta(days=days)
    exp_ts = int(expire.timestamp())
    payload = {"sub": subject, "exp": exp_ts, "typ": "refresh", "ver": token_version}
    token = jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return token, int(timedelta(days=days).total_seconds())


def _decode_token_claims(token: str, expected_type: str) -> dict | None:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("typ") != expected_type:
            return None
        sub = payload.get("sub")
        if sub is None or not isinstance(sub, str):
            return None
        version = payload.get("ver", 0)
        if not isinstance(version, int) or version < 0:
            return None
        return payload
    except JWTError:
        return None


def decode_refresh_claims(token: str) -> dict | None:
    return _decode_token_claims(token, "refresh")


def decode_refresh_token(token: str) -> str | None:
    payload = decode_refresh_claims(token)
    return str(payload["sub"]) if payload else None


def decode_access_claims(token: str) -> dict | None:
    return _decode_token_claims(token, "access")


def decode_access_token(token: str) -> str | None:
    """成功返回 user id (sub)，失败返回 None。"""
    payload = decode_access_claims(token)
    return str(payload["sub"]) if payload else None


def verify_token(token: str) -> bool:
    """兼容旧占位：非空且可解码出 sub 即视为有效。"""
    return decode_access_token(token) is not None
