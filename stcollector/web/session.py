"""대시보드 비밀번호 해시와 세션 토큰. DB나 디스크에 아무것도 쓰지 않는다."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

_N, _R, _P = 2**14, 8, 1
_MAXMEM = 64 * 1024 * 1024
_MAX_ATTEMPTS = 5
_LOCK_S = 300


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(dk)}"


def valid_hash(stored: str) -> bool:
    try:
        kind, n, r, p, salt, dk = stored.split("$")
        return kind == "scrypt" and int(n) >= 2**14 and int(r) > 0 and int(p) > 0 and bool(_unb64(salt)) and bool(_unb64(dk))
    except (ValueError, TypeError):
        return False


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        expected = _unb64(dk)
        actual = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                                maxmem=_MAXMEM, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


class Sessions:
    """서명된 쿠키 토큰. 키는 비밀번호 해시에서 유도해 재시작해도 유지되고,
    비밀번호를 바꾸면 기존 세션이 모두 무효가 된다."""

    def __init__(self, password_hash: str, days: int, clock=time.time):
        self.key = hashlib.sha256(b"stcollector-session\0" + password_hash.encode()).digest()
        self.max_age = days * 86400
        self.clock = clock
        self._revoked: dict[str, float] = {}
        self._lock = threading.Lock()

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self.key, payload.encode(), hashlib.sha256).digest())

    def create(self) -> str:
        payload = f"{int(self.clock()) + self.max_age}.{secrets.token_urlsafe(12)}"
        return f"{payload}.{self._sign(payload)}"

    def valid(self, token: str | None) -> bool:
        if not token or len(token) > 200:
            return False
        parts = token.split(".")
        if len(parts) != 3:
            return False
        payload = f"{parts[0]}.{parts[1]}"
        if not hmac.compare_digest(self._sign(payload), parts[2]):
            return False
        try:
            if int(parts[0]) <= self.clock():
                return False
        except ValueError:
            return False
        with self._lock:
            return parts[1] not in self._revoked

    def revoke(self, token: str | None) -> None:
        if not self.valid(token):
            return
        expires, nonce, _ = token.split(".")
        now = self.clock()
        with self._lock:
            self._revoked = {k: v for k, v in self._revoked.items() if v > now}
            self._revoked[nonce] = float(expires)


class LoginLimiter:
    """IP별 로그인 실패 제한. 5회 실패하면 5분간 거부한다."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self._state: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self.verify_lock = threading.Lock()  # scrypt 는 동시에 하나만 (메모리 보호)

    def blocked(self, ip: str) -> bool:
        with self._lock:
            count, since = self._state.get(ip, (0, 0.0))
            if self.clock() - since > _LOCK_S:
                self._state.pop(ip, None)
                return False
            return count >= _MAX_ATTEMPTS

    def fail(self, ip: str) -> None:
        with self._lock:
            if len(self._state) > 256:
                self._state.clear()
            count, since = self._state.get(ip, (0, self.clock()))
            self._state[ip] = (count + 1, since)

    def success(self, ip: str) -> None:
        with self._lock:
            self._state.pop(ip, None)
