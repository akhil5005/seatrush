"""Signup / login / me, plus the dependencies that guard the other routers.

Two deliberate choices worth defending in an interview:

1. Password hashing uses `hashlib.scrypt` from the standard library. It is a
   memory-hard KDF (unlike a bare SHA-256) so brute-forcing a stolen hash is
   expensive. Using the stdlib avoids a bcrypt/argon2 wheel that needs a C
   toolchain in the image. In production I would reach for argon2id.

2. Auth state lives entirely in the JWT -- there is no server-side session
   table or in-memory session map. That is precisely why the three API replicas
   are interchangeable and why Nginx can round-robin without ip_hash/sticky
   sessions: any replica can verify any token with the shared secret.
"""

import hashlib
import hmac
import os
import re
import time
from dataclasses import dataclass

import jwt
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field, field_validator

from . import config, db
from .errors import ApiError

router = APIRouter(prefix="/api/auth", tags=["auth"])

# scrypt cost parameters. n=2**14 with r=8 needs ~16 MB per hash, which is slow
# enough to matter to an attacker and fast enough (~50 ms) for a login.
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    """Return 'scrypt$n$r$p$salt_hex$hash_hex'.

    The parameters are stored alongside the hash so they can be raised later
    without invalidating existing passwords.
    """
    salt = os.urandom(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
        p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time comparison so we do not leak the hash through timing."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def make_token(user_id: int, role: str) -> str:
    """HS256 JWT with the three claims we actually use.

    `exp` is mandatory: a token with no expiry is a permanent credential. HS256
    (shared secret) is right here because one team owns both the issuer and the
    verifier; RS256 would be the choice if a third party had to verify tokens.
    """
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "role": role, "iat": now,
         "exp": now + config.JWT_TTL_SECONDS},
        config.JWT_SECRET,
        algorithm=config.JWT_ALG,
    )


@dataclass
class User:
    id: int
    role: str


async def current_user(authorization: str | None = Header(default=None)) -> User:
    """Dependency for every authenticated route.

    Note it does NOT hit the database: the token is self-contained, so an
    authenticated request costs one HMAC verification. The cost is that a
    revoked user stays valid until `exp`; with a 24 h TTL that is acceptable
    for this app, and the fix (a deny-list) is listed in the limitations.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "unauthorized", "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        # `algorithms=` is pinned: without it a caller could present an
        # alg=none token and be trusted. Classic JWT footgun.
        claims = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise ApiError(401, "token_expired", "Session expired, please log in again")
    except jwt.InvalidTokenError:
        raise ApiError(401, "unauthorized", "Invalid token")
    return User(id=int(claims["sub"]), role=claims.get("role", "user"))


async def current_admin(user: User = Depends(current_user)) -> User:
    """Role check for /api/admin/* and /api/ops/*."""
    if user.role != "admin":
        raise ApiError(403, "forbidden", "Admin role required")
    return user


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

# A small regex instead of pydantic's EmailStr: EmailStr pulls in
# email-validator + dnspython, and this app never needs deliverability checks.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


class Credentials(BaseModel):
    email: str = Field(min_length=5, max_length=200)
    password: str = Field(min_length=6, max_length=200)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("not a valid email address")
        return v


def _token_response(user_id: int, email: str, role: str) -> dict:
    return {
        "token": make_token(user_id, role),
        "user": {"id": user_id, "email": email, "role": role},
    }


@router.post("/signup", status_code=201)
async def signup(body: Credentials):
    """Self-signup always creates a plain 'user'; roles are never client-chosen."""
    email = body.email.strip().lower()
    row = await db.pool().fetchrow(
        """
        INSERT INTO users (email, password_hash, role)
        VALUES ($1, $2, 'user')
        -- The UNIQUE index on email is the real guard against a duplicate
        -- account, including two simultaneous signups with the same address.
        ON CONFLICT (email) DO NOTHING
        RETURNING id, email, role
        """,
        email, hash_password(body.password),
    )
    if row is None:
        raise ApiError(409, "email_taken", "That email is already registered")
    return _token_response(row["id"], row["email"], row["role"])


@router.post("/login")
async def login(body: Credentials):
    email = body.email.strip().lower()
    row = await db.pool().fetchrow(
        "SELECT id, email, role, password_hash FROM users WHERE email = $1", email
    )
    # One generic message for "no such user" and "wrong password" so the
    # endpoint cannot be used to enumerate which emails have accounts.
    # Nginx also rate-limits this path to ~5 requests/minute per IP.
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise ApiError(401, "invalid_credentials", "Email or password is incorrect")
    return _token_response(row["id"], row["email"], row["role"])


@router.get("/me")
async def me(user: User = Depends(current_user)):
    row = await db.pool().fetchrow(
        "SELECT id, email, role, created_at FROM users WHERE id = $1", user.id
    )
    if row is None:
        raise ApiError(401, "unauthorized", "User no longer exists")
    return {
        "id": row["id"],
        "email": row["email"],
        "role": row["role"],
        "created_at": row["created_at"],
    }
