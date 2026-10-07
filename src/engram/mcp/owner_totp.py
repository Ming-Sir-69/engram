"""Shared owner TOTP verification with a privately configured authenticator.

No secret or generated OTP is embedded.
OAuth state remains per service. A code may authorize separate services once each,
while each service rejects replay and persists global failure lockout in SQLite.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import stat
import struct
import time
from pathlib import Path

TOTP_MAX_FAILS = 5
TOTP_WINDOW = 15 * 60
TOTP_LOCK = 15 * 60
TOTP_LABEL = "Engram owner authenticator"


def read_secret(path):
    """Accept only a private regular seed file, never return errors containing it."""
    if not path:
        return None
    try:
        file = Path(path)
        if file.is_symlink():
            return None
        info = file.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            return None
        seed = file.read_text().strip().upper()
        # Validate inside the process; never print the value or a seed fingerprint.
        key = base64.b32decode(seed + "=" * (-len(seed) % 8))
        return seed if len(key) >= 10 else None
    except (OSError, UnicodeError, ValueError):
        return None


def totp_now(secret_b32, at=None, step=30):
    """RFC 6238 SHA-1, six ASCII digits, 30-second periods."""
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    counter = int((time.time() if at is None else at) // step)
    value = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = value[-1] & 15
    digits = (struct.unpack(">I", value[offset:offset + 4])[0] & 0x7fffffff) % 1000000
    return counter, f"{digits:06d}"


def check_totp(provider, connection, code, *, now):
    """Called under the provider's BEGIN IMMEDIATE transaction."""
    secret = read_secret(provider.totp_file)
    if not secret:
        return False, "本服务尚未配置统一验证器，请让已认证的本人助手批准"
    fails = [t for t in (provider.get("totp", "fails", c=connection) or []) if t > now - TOTP_WINDOW]
    if len(fails) >= TOTP_MAX_FAILS and fails[-1] > now - TOTP_LOCK:
        return False, f"错误次数过多，已锁定 {int((fails[-1] + TOTP_LOCK - now) // 60 + 1)} 分钟"
    normalized = (code or "").replace(" ", "").strip()
    valid_shape = len(normalized) == 6 and all(ch in "0123456789" for ch in normalized)
    last = (provider.get("totp", "last", c=connection) or {}).get("counter", -1)
    for at in (now - 30, now, now + 30):
        counter, expected = totp_now(secret, at)
        if valid_shape and hmac.compare_digest(normalized, expected):
            if counter <= last:
                return False, "这个验证码刚用过（本服务），请等下一个"
            provider.put(connection, "totp", "last", {"counter": counter}, 32503680000)
            provider.put(connection, "totp", "fails", [], 32503680000)
            return True, ""
    fails.append(now)
    provider.put(connection, "totp", "fails", fails, 32503680000)
    return False, f"验证码不对（窗口内已错 {len(fails)} 次，{TOTP_MAX_FAILS} 次后锁定）"
