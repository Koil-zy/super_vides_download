"""用户注册 / 登录 / 退出 / 会话。

密码用标准库 hashlib.scrypt 加盐哈希，会话 token 用 secrets 随机生成后
写进 HttpOnly Cookie；数据库里只存 token 本身（有效期 30 天）。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timezone

from fastapi import Request

from .db import get_db, iso_after, now_iso

SESSION_COOKIE = 'sv_session'
SESSION_DAYS = 30

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1


class AuthError(Exception):
    """可直接返回给前端的业务错误。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                            p=_SCRYPT_P, dklen=32)
    return f'scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}'


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, digest_hex = stored.split('$')
        if algo != 'scrypt':
            return False
        digest = hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=len(digest_hex) // 2)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _normalize(email: str, password: str, nickname: str | None) -> tuple[str, str, str]:
    email = (email or '').strip().lower()
    password = password or ''
    nickname = (nickname or '').strip()
    if not _EMAIL_RE.match(email):
        raise AuthError('invalid_email', '请输入正确的邮箱地址')
    if not 6 <= len(password) <= 64:
        raise AuthError('invalid_password', '密码长度需在 6 ~ 64 位之间')
    if len(nickname) > 16:
        raise AuthError('invalid_nickname', '昵称最多 16 个字')
    if not nickname:
        nickname = email.split('@')[0][:16]
    return email, password, nickname


def user_payload(row: sqlite3.Row) -> dict:
    return {
        'id': row['id'],
        'email': row['email'],
        'nickname': row['nickname'],
        'created_at': row['created_at'],
    }


def register(conn: sqlite3.Connection, email: str, password: str, nickname: str | None) -> dict:
    email, password, nickname = _normalize(email, password, nickname)
    exists = conn.execute('SELECT 1 FROM users WHERE email = ?', (email,)).fetchone()
    if exists:
        raise AuthError('email_exists', '该邮箱已注册，请直接登录')
    cur = conn.execute(
        'INSERT INTO users (email, password_hash, nickname, created_at) VALUES (?, ?, ?, ?)',
        (email, hash_password(password), nickname, now_iso()),
    )
    row = conn.execute('SELECT * FROM users WHERE id = ?', (cur.lastrowid,)).fetchone()
    return user_payload(row)


def login(conn: sqlite3.Connection, email: str, password: str) -> dict:
    email = (email or '').strip().lower()
    row = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    if not row or not verify_password(password or '', row['password_hash']):
        raise AuthError('invalid_credentials', '邮箱或密码不正确', 401)
    return user_payload(row)


def create_session(conn: sqlite3.Connection, user_id: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires_at = iso_after(days=SESSION_DAYS)
    conn.execute(
        'INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)',
        (token, user_id, now_iso(), expires_at),
    )
    conn.execute('DELETE FROM sessions WHERE expires_at <= ?', (now_iso(),))
    return token, expires_at


def drop_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute('DELETE FROM sessions WHERE token = ?', (token,))


def current_user(request: Request) -> dict | None:
    """从 Cookie 还原当前用户；未登录或会话过期返回 None。"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with get_db() as conn:
        row = conn.execute(
            'SELECT u.*, s.expires_at AS session_expires FROM sessions s '
            'JOIN users u ON u.id = s.user_id WHERE s.token = ?', (token,)).fetchone()
        if not row:
            return None
        if row['session_expires'] <= now_iso():
            conn.execute('DELETE FROM sessions WHERE token = ?', (token,))
            return None
        return user_payload(row)


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise AuthError('login_required', '请先登录后再下载视频', 401)
    return user


def set_session_cookie(response, token: str) -> None:
    # 本地 http 调试用 secure=False；部署到 https 后可改成 secure=True
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite='lax',
        path='/',
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path='/')


def session_expiry_text() -> str:
    """给前端展示用的会话到期时间。"""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')
