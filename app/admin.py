"""管理端账号：JWT 认证、角色权限与初始超级管理员。

- 账号存放在 admins 表，角色分 superadmin（超级管理员）/ merchant（商户）
- 登录态用 JWT，放在 HttpOnly Cookie `sv_admin`，同时兼容 Authorization: Bearer
- 密钥、初始超管账号均可用环境变量覆盖（配置文件即本文件顶部常量）
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Request

from .auth import hash_password, verify_password
from .db import get_db, now_iso

ADMIN_COOKIE = 'sv_admin'
JWT_SECRET = os.getenv('ADMIN_JWT_SECRET', 'sv-admin-dev-secret-change-me-please-32bytes')
JWT_ALG = 'HS256'
JWT_HOURS = 12

# 初始超级管理员：仅当库里还没有超管时自动创建；上线前请修改
INIT_PHONE = os.getenv('ADMIN_INIT_PHONE', '13800138000')
INIT_PASSWORD = os.getenv('ADMIN_INIT_PASSWORD', 'Admin@123456')
INIT_NAME = os.getenv('ADMIN_INIT_NAME', '超级管理员')
# 自助注册超级管理员时需要的初始化口令；留空则关闭超管自助注册
INIT_CODE = os.getenv('ADMIN_INIT_CODE', INIT_PASSWORD)

ROLE_LABELS = {'superadmin': '超级管理员', 'merchant': '商户'}

_PHONE_RE_CHARS = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'


class AdminError(Exception):
    """可直接返回给前端的业务错误。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def admin_payload(row: sqlite3.Row) -> dict:
    return {
        'id': row['id'],
        'phone': row['phone'],
        'name': row['name'],
        'role': row['role'],
        'role_label': ROLE_LABELS.get(row['role'], row['role']),
        'merchant_code': row['merchant_code'],
        'created_at': row['created_at'],
    }


def _new_merchant_code(conn: sqlite3.Connection) -> str:
    while True:
        code = ''.join(secrets.choice(_PHONE_RE_CHARS) for _ in range(6))
        if not conn.execute('SELECT 1 FROM admins WHERE merchant_code = ?', (code,)).fetchone():
            return code


def create_admin(conn: sqlite3.Connection, phone: str, password: str, name: str,
                 role: str = 'merchant') -> dict:
    if role not in ROLE_LABELS:
        raise AdminError('invalid_role', '未知的管理端角色')
    if conn.execute('SELECT 1 FROM admins WHERE phone = ?', (phone,)).fetchone():
        raise AdminError('phone_exists', '该手机号已注册管理端账号')
    cur = conn.execute(
        'INSERT INTO admins (phone, password_hash, name, role, merchant_code, status, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (phone, hash_password(password), name or '', role, _new_merchant_code(conn), 'active', now_iso()),
    )
    return admin_payload(conn.execute('SELECT * FROM admins WHERE id = ?', (cur.lastrowid,)).fetchone())


def check_init_code(code: str) -> None:
    """校验超级管理员自助注册的初始化口令。"""
    if not INIT_CODE:
        raise AdminError('register_closed', '当前未开放超级管理员自助注册', 403)
    if not secrets.compare_digest((code or '').strip(), INIT_CODE):
        raise AdminError('invalid_init_code', '管理员初始化口令不正确', 403)


def login_admin(conn: sqlite3.Connection, phone: str, password: str) -> dict:
    row = conn.execute('SELECT * FROM admins WHERE phone = ?', (phone,)).fetchone()
    if not row or not verify_password(password or '', row['password_hash']):
        raise AdminError('invalid_credentials', '手机号或密码不正确', 401)
    if row['status'] != 'active':
        raise AdminError('account_disabled', '账号已被停用，请联系超级管理员', 403)
    return admin_payload(row)


def ensure_super_admin(conn: sqlite3.Connection) -> None:
    """首次启动时创建默认超级管理员，已有超管则跳过。"""
    if conn.execute("SELECT 1 FROM admins WHERE role = 'superadmin'").fetchone():
        return
    create_admin(conn, INIT_PHONE, INIT_PASSWORD, INIT_NAME, role='superadmin')


# ------------------------------------------------------------------ JWT


def create_token(admin: dict) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        'sub': str(admin['id']),
        'role': admin['role'],
        'phone': admin['phone'],
        'iat': now,
        'exp': now + timedelta(hours=JWT_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def _token_from_request(request: Request) -> str | None:
    header = request.headers.get('Authorization') or ''
    if header.lower().startswith('bearer '):
        return header[7:].strip()
    return request.cookies.get(ADMIN_COOKIE)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError as exc:
        raise AdminError('token_expired', '登录已过期，请重新登录', 401) from exc
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise AdminError('invalid_token', '登录状态无效，请重新登录', 401) from exc
    return payload


def current_admin(request: Request) -> dict | None:
    token = _token_from_request(request)
    if not token:
        return None
    try:
        payload = decode_token(token)
    except AdminError:
        return None
    with get_db() as conn:
        row = conn.execute('SELECT * FROM admins WHERE id = ?',
                           (int(payload.get('sub') or 0),)).fetchone()
        if not row or row['status'] != 'active':
            return None
        return admin_payload(row)


def require_admin(request: Request, roles: tuple[str, ...] | None = None) -> dict:
    admin = current_admin(request)
    if not admin:
        raise AdminError('login_required', '请先登录管理端', 401)
    if roles and admin['role'] not in roles:
        raise AdminError('forbidden', '当前账号无权访问该功能', 403)
    return admin


def set_admin_cookie(response, token: str) -> None:
    response.set_cookie(
        ADMIN_COOKIE, token,
        max_age=JWT_HOURS * 3600,
        httponly=True,
        samesite='lax',
        path='/',
    )


def clear_admin_cookie(response) -> None:
    response.delete_cookie(ADMIN_COOKIE, path='/')
