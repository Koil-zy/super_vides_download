"""管理端接口：JWT 登录 / 短信验证码 / 用户管理 / 黑名单 / 下载记录 / 支付配置。

权限模型：
- superadmin：用户增删改、黑名单、下载记录；不能访问支付配置
- merchant：支付配置增改删；只能只读查看归属自己的用户
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import admin, auth, pay, sms
from .db import get_db, iso_after, now_iso

router = APIRouter(prefix='/api/admin', tags=['admin'])


def api_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {'code': code, 'message': message})


def require_admin(request: Request) -> dict:
    try:
        return admin.require_admin(request)
    except admin.AdminError as exc:
        raise api_error(exc.status, exc.code, exc.message) from exc


def admin_only(request: Request) -> dict:
    info = require_admin(request)
    if info['role'] != 'superadmin':
        raise api_error(403, 'forbidden', '当前账号无权访问该功能')
    return info


def merchant_only(request: Request) -> dict:
    info = require_admin(request)
    if info['role'] != 'merchant':
        raise api_error(403, 'forbidden', '当前账号无权访问该功能')
    return info


# ------------------------------------------------------------------ 模型


class SmsIn(BaseModel):
    phone: str
    scene: str = 'register'


class AdminRegisterIn(BaseModel):
    phone: str
    password: str
    code: str
    name: str | None = None
    role: str = 'merchant'
    init_code: str | None = None


class AdminLoginIn(BaseModel):
    phone: str
    password: str


class UserCreateIn(BaseModel):
    email: str
    password: str
    nickname: str | None = None
    merchant_id: int | None = None
    premium: bool = False


class PremiumIn(BaseModel):
    premium: bool


class BlacklistIn(BaseModel):
    reason: str | None = None


class PayConfigIn(BaseModel):
    provider: str
    app_id: str = ''
    mchid: str = ''
    api_key: str = ''
    private_key: str = ''
    public_key: str = ''


# ------------------------------------------------------------------ 通用


def _paging(page: int, size: int) -> tuple[int, int]:
    page = max(1, page)
    size = min(100, max(1, size))
    return size, (page - 1) * size


def _page_payload(items: list[dict], total: int, page: int, size: int) -> dict:
    return {'items': items, 'total': total, 'page': page, 'size': size,
            'pages': max(1, (total + size - 1) // size)}


def _validate_password(password: str) -> None:
    if not 6 <= len(password or '') <= 64:
        raise api_error(400, 'invalid_password', '密码长度需在 6 ~ 64 位之间')


def _user_payload(row: sqlite3.Row) -> dict:
    data = dict(row)
    premium_until = data.get('premium_until') or ''
    return {
        'id': data['id'],
        'email': data['email'],
        'nickname': data['nickname'],
        'created_at': data['created_at'],
        'merchant_id': data.get('merchant_id'),
        'merchant_name': data.get('merchant_name') or '',
        'blacklisted': bool(data.get('blacklisted_at')),
        'blacklisted_at': data.get('blacklisted_at') or '',
        'blacklist_reason': data.get('blacklist_reason') or '',
        'premium': bool(premium_until),
        'premium_until': premium_until,
    }


_USER_SELECT = (
    'SELECT u.id, u.email, u.nickname, u.created_at, u.merchant_id, '
    'u.blacklisted_at, u.blacklist_reason, a.name AS merchant_name, '
    '(SELECT MAX(s.expires_at) FROM subscriptions s '
    ' WHERE s.user_id = u.id AND s.expires_at > ?) AS premium_until '
    'FROM users u LEFT JOIN admins a ON a.id = u.merchant_id '
)


def _fetch_user(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute(_USER_SELECT + 'WHERE u.id = ?', (now_iso(), user_id)).fetchone()
    if not row:
        raise api_error(404, 'user_not_found', '用户不存在')
    return _user_payload(row)


def _grant_days(conn: sqlite3.Connection, user_id: int, days: int = 30) -> None:
    """开通/顺延会员：已有有效订阅则从原到期日顺延。"""
    active = conn.execute(
        'SELECT MAX(expires_at) AS expires_at FROM subscriptions '
        'WHERE user_id = ? AND expires_at > ?', (user_id, now_iso())).fetchone()
    if active and active['expires_at']:
        base = datetime.strptime(active['expires_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        expires_at = (base + timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    else:
        expires_at = iso_after(days=days)
    conn.execute(
        'INSERT INTO subscriptions (user_id, plan, started_at, expires_at, order_id) '
        'VALUES (?, ?, ?, ?, NULL)',
        (user_id, pay.PLAN['id'], now_iso(), expires_at),
    )


# ------------------------------------------------------------------ 登录 / 注册


@router.post('/sms/send')
async def sms_send(body: SmsIn):
    try:
        result = sms.send_code(body.phone, body.scene)
    except sms.SmsError as exc:
        raise api_error(exc.status, exc.code, exc.message) from exc
    return {'ok': True, 'interval': sms.RESEND_INTERVAL, **result}


@router.post('/register')
async def admin_register(body: AdminRegisterIn):
    _validate_password(body.password)
    role = (body.role or 'merchant').strip()
    if role not in admin.ROLE_LABELS:
        raise api_error(400, 'invalid_role', '未知的管理端角色')
    if role == 'superadmin':
        try:
            admin.check_init_code(body.init_code or '')
        except admin.AdminError as exc:
            raise api_error(exc.status, exc.code, exc.message) from exc
    try:
        sms.verify_code(body.phone, 'register', body.code)
    except sms.SmsError as exc:
        raise api_error(exc.status, exc.code, exc.message) from exc
    try:
        with get_db() as conn:
            info = admin.create_admin(conn, body.phone.strip(), body.password,
                                      (body.name or '').strip(), role=role)
    except admin.AdminError as exc:
        raise api_error(exc.status, exc.code, exc.message) from exc
    token = admin.create_token(info)
    resp = JSONResponse({'admin': info, 'roles': admin.ROLE_LABELS})
    admin.set_admin_cookie(resp, token)
    return resp


@router.post('/login')
async def admin_login(body: AdminLoginIn):
    try:
        with get_db() as conn:
            info = admin.login_admin(conn, body.phone.strip(), body.password)
    except admin.AdminError as exc:
        raise api_error(exc.status, exc.code, exc.message) from exc
    token = admin.create_token(info)
    resp = JSONResponse({'admin': info, 'roles': admin.ROLE_LABELS})
    admin.set_admin_cookie(resp, token)
    return resp


@router.post('/logout')
async def admin_logout():
    resp = JSONResponse({'ok': True})
    admin.clear_admin_cookie(resp)
    return resp


@router.get('/me')
async def admin_me(request: Request):
    info = require_admin(request)
    return {'admin': info, 'roles': admin.ROLE_LABELS}


# ------------------------------------------------------------------ 用户管理


@router.get('/merchants')
async def list_merchants(request: Request):
    """超级管理员新增用户/查看归属时需要的商户列表。"""
    admin_only(request)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, phone, name, merchant_code, created_at FROM admins "
            "WHERE role = 'merchant' AND status = 'active' ORDER BY id DESC").fetchall()
    return {'items': [dict(r) for r in rows]}


@router.get('/users')
async def list_users(request: Request, q: str = '', page: int = 1, size: int = 20):
    info = require_admin(request)
    limit, offset = _paging(page, size)
    filters: list = []
    where: list[str] = []
    if info['role'] == 'merchant':
        # 商户只能看归属自己的用户；黑名单用户也会标出状态（只读）
        where.append('u.merchant_id = ?')
        filters.append(info['id'])
    else:
        # 超级管理员的用户管理不包含黑名单，黑名单单独一页
        where.append('u.blacklisted_at IS NULL')
    keyword = (q or '').strip()
    if keyword:
        search = f'%{keyword}%'
        where.append('(u.email LIKE ? OR u.nickname LIKE ?)')
        filters.extend([search, search])
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''
    with get_db() as conn:
        total = conn.execute(
            'SELECT COUNT(*) AS n FROM users u ' + where_sql, filters).fetchone()['n']
        rows = conn.execute(
            _USER_SELECT + where_sql + ' ORDER BY u.id DESC LIMIT ? OFFSET ?',
            [now_iso(), *filters, limit, offset],
        ).fetchall()
    return _page_payload([_user_payload(r) for r in rows], total, page, limit)


@router.post('/users')
async def create_user(body: UserCreateIn, request: Request):
    admin_only(request)
    _validate_password(body.password)
    with get_db() as conn:
        if body.merchant_id is not None:
            if not conn.execute('SELECT 1 FROM admins WHERE id = ?', (body.merchant_id,)).fetchone():
                raise api_error(400, 'invalid_merchant', '指定的商户不存在')
        try:
            user = auth.register(conn, body.email, body.password, body.nickname)
        except auth.AuthError as exc:
            raise api_error(exc.status, exc.code, exc.message) from exc
        if body.merchant_id is not None:
            conn.execute('UPDATE users SET merchant_id = ? WHERE id = ?',
                         (body.merchant_id, user['id']))
        if body.premium:
            _grant_days(conn, user['id'], pay.PLAN['days'])
        result = _fetch_user(conn, user['id'])
    return {'user': result}


@router.patch('/users/{user_id}/premium')
async def set_user_premium(user_id: int, body: PremiumIn, request: Request):
    admin_only(request)
    with get_db() as conn:
        _fetch_user(conn, user_id)
        if body.premium:
            active = conn.execute(
                'SELECT 1 FROM subscriptions WHERE user_id = ? AND expires_at > ?',
                (user_id, now_iso())).fetchone()
            if not active:
                _grant_days(conn, user_id, pay.PLAN['days'])
        else:
            conn.execute(
                'UPDATE subscriptions SET expires_at = ? WHERE user_id = ? AND expires_at > ?',
                (now_iso(), user_id, now_iso()),
            )
        result = _fetch_user(conn, user_id)
    return {'user': result}


@router.delete('/users/{user_id}')
async def blacklist_user(user_id: int, request: Request, body: BlacklistIn | None = None):
    """逻辑删除：只写入黑名单，不物理删除用户。"""
    admin_only(request)
    with get_db() as conn:
        _fetch_user(conn, user_id)
        reason = (body.reason if body and body.reason else '').strip()[:200]
        conn.execute(
            'UPDATE users SET blacklisted_at = ?, blacklist_reason = ? WHERE id = ?',
            (now_iso(), reason, user_id),
        )
        conn.execute('DELETE FROM sessions WHERE user_id = ?', (user_id,))
    return {'ok': True, 'blacklisted': True}


@router.post('/users/{user_id}/restore')
async def restore_user(user_id: int, request: Request):
    """移出黑名单：恢复后用户可以正常登录和下载。"""
    admin_only(request)
    with get_db() as conn:
        _fetch_user(conn, user_id)
        conn.execute(
            'UPDATE users SET blacklisted_at = NULL, blacklist_reason = ? WHERE id = ?',
            ('', user_id),
        )
        result = _fetch_user(conn, user_id)
    return {'user': result}


@router.get('/blacklist')
async def list_blacklist(request: Request, q: str = '', page: int = 1, size: int = 20):
    admin_only(request)
    limit, offset = _paging(page, size)
    filters: list = []
    where = ['u.blacklisted_at IS NOT NULL']
    keyword = (q or '').strip()
    if keyword:
        search = f'%{keyword}%'
        where.append('(u.email LIKE ? OR u.nickname LIKE ?)')
        filters.extend([search, search])
    where_sql = 'WHERE ' + ' AND '.join(where)
    with get_db() as conn:
        total = conn.execute(
            'SELECT COUNT(*) AS n FROM users u ' + where_sql, filters).fetchone()['n']
        rows = conn.execute(
            _USER_SELECT + where_sql + ' ORDER BY u.blacklisted_at DESC LIMIT ? OFFSET ?',
            [now_iso(), *filters, limit, offset],
        ).fetchall()
    return _page_payload([_user_payload(r) for r in rows], total, page, limit)


# ------------------------------------------------------------------ 下载记录（只读）


@router.get('/downloads')
async def list_downloads(request: Request, q: str = '', page: int = 1, size: int = 20):
    admin_only(request)
    limit, offset = _paging(page, size)
    filters: list = []
    where: list[str] = []
    keyword = (q or '').strip()
    if keyword:
        search = f'%{keyword}%'
        where.append('(u.email LIKE ? OR u.nickname LIKE ?)')
        filters.extend([search, search])
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''
    with get_db() as conn:
        total = conn.execute(
            'SELECT COUNT(*) AS n FROM download_records d '
            'JOIN users u ON u.id = d.user_id ' + where_sql, filters).fetchone()['n']
        rows = conn.execute(
            'SELECT d.id, d.title, d.platform, d.format, d.height, d.created_at, '
            'd.user_id, u.email, u.nickname '
            'FROM download_records d JOIN users u ON u.id = d.user_id ' + where_sql +
            ' ORDER BY d.created_at DESC, d.id DESC LIMIT ? OFFSET ?',
            [*filters, limit, offset],
        ).fetchall()
    items = [{
        'id': r['id'],
        'user_id': r['user_id'],
        'email': r['email'],
        'nickname': r['nickname'],
        'title': r['title'],
        'platform': r['platform'],
        'quality': f"{r['height']}P" if r['height'] else '',
        'format': r['format'],
        'created_at': r['created_at'],
    } for r in rows]
    return _page_payload(items, total, page, limit)


def record_download(user_id: int, job_id: str, title: str, platform: str,
                    fmt: str, height: int, url: str) -> None:
    """下载任务创建时落一条记录，供管理端下载记录页查看。"""
    with get_db() as conn:
        user = conn.execute('SELECT merchant_id FROM users WHERE id = ?', (user_id,)).fetchone()
        if not user:
            return
        conn.execute(
            'INSERT INTO download_records '
            '(user_id, merchant_id, job_id, title, platform, format, height, url, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (user_id, user['merchant_id'], job_id, title or '未命名视频', platform or '',
             fmt or '', height or 0, url or '', now_iso()),
        )


# ------------------------------------------------------------------ 支付配置（商户）


PROVIDER_REQUIRED = {
    'wechat': ('app_id', 'mchid', 'api_key'),
    'alipay': ('app_id', 'private_key', 'public_key'),
}
FIELD_LABELS = {
    'app_id': 'AppID',
    'mchid': '商户号',
    'api_key': 'APIv3 密钥',
    'private_key': '应用私钥',
    'public_key': '支付宝公钥',
}
STATUS_LABELS = {'active': '当前生效', 'superseded': '历史版本', 'deleted': '已删除'}


def _mask(value: str) -> str:
    value = value or ''
    if not value:
        return ''
    if len(value) <= 8:
        return '••••'
    return f'{value[:4]}••••{value[-4:]}'


def _config_payload(row: sqlite3.Row, full: bool = False) -> dict:
    data = dict(row)
    payload = {
        'id': data['id'],
        'provider': data['provider'],
        'provider_label': pay.PROVIDERS[data['provider']]['label'],
        'status': data['status'],
        'status_label': STATUS_LABELS.get(data['status'], data['status']),
        'created_at': data['created_at'],
        'deleted_at': data['deleted_at'],
        'editable': data['status'] == 'active',
        'deletable': data['status'] != 'deleted',
    }
    for field in ('app_id', 'mchid', 'api_key', 'private_key', 'public_key'):
        payload[field] = data[field] if full else _mask(data[field])
    return payload


def _validate_config(body: PayConfigIn) -> None:
    if body.provider not in pay.PROVIDERS:
        raise api_error(400, 'invalid_provider', '不支持的支付方式')
    missing = [
        FIELD_LABELS[field] for field in PROVIDER_REQUIRED[body.provider]
        if not (getattr(body, field) or '').strip()
    ]
    if missing:
        raise api_error(400, 'missing_fields', f'请填写：{"、".join(missing)}')


def _insert_config(conn: sqlite3.Connection, admin_id: int, body: PayConfigIn) -> int:
    conn.execute(
        'UPDATE payment_configs SET status = ? WHERE admin_id = ? AND provider = ? AND status = ?',
        ('superseded', admin_id, body.provider, 'active'),
    )
    cur = conn.execute(
        'INSERT INTO payment_configs '
        '(admin_id, provider, app_id, mchid, api_key, private_key, public_key, status, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (admin_id, body.provider, body.app_id.strip(), body.mchid.strip(),
         body.api_key.strip(), body.private_key.strip(), body.public_key.strip(),
         'active', now_iso()),
    )
    return cur.lastrowid


def _own_config(conn: sqlite3.Connection, admin_id: int, config_id: int) -> sqlite3.Row:
    row = conn.execute('SELECT * FROM payment_configs WHERE id = ? AND admin_id = ?',
                       (config_id, admin_id)).fetchone()
    if not row:
        raise api_error(404, 'config_not_found', '配置不存在')
    return row


@router.get('/pay/configs')
async def list_pay_configs(request: Request):
    info = merchant_only(request)
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM payment_configs WHERE admin_id = ? AND status != ? '
            'ORDER BY provider, id DESC', (info['id'], 'deleted')).fetchall()
    return {'items': [_config_payload(r) for r in rows], 'providers': pay.PROVIDERS}


@router.get('/pay/configs/{config_id}')
async def get_pay_config(config_id: int, request: Request):
    info = merchant_only(request)
    with get_db() as conn:
        row = _own_config(conn, info['id'], config_id)
    return {'config': _config_payload(row, full=True)}


@router.post('/pay/configs')
async def create_pay_config(body: PayConfigIn, request: Request):
    info = merchant_only(request)
    _validate_config(body)
    with get_db() as conn:
        new_id = _insert_config(conn, info['id'], body)
        row = conn.execute('SELECT * FROM payment_configs WHERE id = ?', (new_id,)).fetchone()
    return {'config': _config_payload(row)}


@router.put('/pay/configs/{config_id}')
async def update_pay_config(config_id: int, body: PayConfigIn, request: Request):
    """只允许修改当前生效的配置；修改 = 新增一条版本，旧记录标记为历史。"""
    info = merchant_only(request)
    with get_db() as conn:
        current = _own_config(conn, info['id'], config_id)
        if current['status'] != 'active':
            raise api_error(400, 'config_readonly', '历史配置不可修改，请新增当前生效的配置')
        if body.provider != current['provider']:
            raise api_error(400, 'provider_mismatch', '支付方式不可变更，请重新新增配置')
        _validate_config(body)
        new_id = _insert_config(conn, info['id'], body)
        row = conn.execute('SELECT * FROM payment_configs WHERE id = ?', (new_id,)).fetchone()
    return {'config': _config_payload(row)}


@router.delete('/pay/configs/{config_id}')
async def delete_pay_config(config_id: int, request: Request):
    info = merchant_only(request)
    with get_db() as conn:
        row = _own_config(conn, info['id'], config_id)
        conn.execute(
            'UPDATE payment_configs SET status = ?, deleted_at = ? WHERE id = ?',
            ('deleted', now_iso(), row['id']),
        )
    return {'ok': True}
