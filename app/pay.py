"""二维码支付（微信 / 支付宝）+ 会员订阅。

当前为测试实现：支付渠道密钥是伪造的占位值，二维码内容也是模拟的；
真实接入时只需替换本文件顶部的常量，并补上异步回调验签（见 notify 注释）。
"""

from __future__ import annotations

import io
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

import qrcode
from qrcode.image.svg import SvgPathImage

from .db import iso_after, now_iso

# ------------------------------------------------------------------ 支付渠道配置
# 测试期使用伪造密钥；正式上线时用环境变量覆盖下面 4 个常量即可，无需改代码。
WXPAY_APPID = os.getenv('WXPAY_APPID', 'wx_mock_appid_replace_me')
WXPAY_MCHID = os.getenv('WXPAY_MCHID', '1900000001_mock_mchid')
WXPAY_API_KEY = os.getenv('WXPAY_API_KEY', 'mock_wxpay_apiv3_key_replace_me')
ALIPAY_APPID = os.getenv('ALIPAY_APPID', '2021000000000000_mock_appid')
ALIPAY_PRIVATE_KEY = os.getenv('ALIPAY_PRIVATE_KEY', 'mock_alipay_private_key_replace_me')
ALIPAY_PUBLIC_KEY = os.getenv('ALIPAY_PUBLIC_KEY', 'mock_alipay_public_key_replace_me')

# PAY_MOCK=0 时关闭「模拟支付成功」按钮（接入真实渠道后设置）
PAY_MOCK = os.getenv('PAY_MOCK', '1') != '0'

PROVIDERS = {
    'wechat': {'key': 'wechat', 'label': '微信支付', 'color': '#2AC864'},
    'alipay': {'key': 'alipay', 'label': '支付宝', 'color': '#1677FF'},
}

PLAN = {
    'id': 'monthly',
    'label': '月度会员',
    'amount_cents': 1500,
    'price_text': '¥15',
    'days': 30,
    'desc': '30 天内可下载 720P / 1080P / 4K 等全部清晰度',
}


class PayError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ------------------------------------------------------------------ 会员订阅


def active_subscription(conn: sqlite3.Connection, user_id: int) -> dict | None:
    row = conn.execute(
        'SELECT * FROM subscriptions WHERE user_id = ? AND expires_at > ? '
        'ORDER BY expires_at DESC LIMIT 1', (user_id, now_iso())).fetchone()
    if not row:
        return None
    return subscription_payload(row)


def subscription_payload(row: sqlite3.Row) -> dict:
    expires = datetime.strptime(row['expires_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    days_left = max(0, (expires - datetime.now(timezone.utc)).days + 1)
    return {
        'plan': row['plan'],
        'plan_label': PLAN['label'] if row['plan'] == PLAN['id'] else row['plan'],
        'started_at': row['started_at'],
        'expires_at': row['expires_at'],
        'days_left': days_left,
        'active': True,
    }


def _grant_subscription(conn: sqlite3.Connection, user_id: int, order_id: str) -> dict:
    """支付成功后写入订阅；已有会员则从原到期时间顺延。"""
    current = conn.execute(
        'SELECT * FROM subscriptions WHERE user_id = ? AND expires_at > ? '
        'ORDER BY expires_at DESC LIMIT 1', (user_id, now_iso())).fetchone()
    started_at = now_iso()
    if current:
        base = datetime.strptime(current['expires_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        expires_at = (base + timedelta(days=PLAN['days'])).strftime('%Y-%m-%dT%H:%M:%SZ')
    else:
        expires_at = iso_after(days=PLAN['days'])
    conn.execute(
        'INSERT INTO subscriptions (user_id, plan, started_at, expires_at, order_id) VALUES (?, ?, ?, ?, ?)',
        (user_id, PLAN['id'], started_at, expires_at, order_id),
    )
    row = conn.execute(
        'SELECT * FROM subscriptions WHERE order_id = ? ORDER BY id DESC LIMIT 1', (order_id,)).fetchone()
    return subscription_payload(row)


def subscription_info(conn: sqlite3.Connection, user_id: int) -> dict:
    active = active_subscription(conn, user_id)
    return {'premium': bool(active), 'subscription': active}


# ------------------------------------------------------------------ 订单 / 二维码


def _qr_content(provider: str) -> str:
    """模拟渠道返回的二维码内容；真实接入时换成微信 code_url / 支付宝 qr_code。"""
    code = secrets.token_hex(10).upper()
    if provider == 'wechat':
        return f'weixin://wxpay/bizpayurl?pr={code}'
    return f'https://qr.alipay.com/{code}'


def create_order(conn: sqlite3.Connection, user_id: int, provider: str) -> dict:
    if provider not in PROVIDERS:
        raise PayError('invalid_provider', '不支持的支付方式')
    order_id = 'ORD' + secrets.token_hex(8).upper()
    # 真实接入：这里应调用微信「Native 下单」或支付宝「当面付预下单」获取 qr_code
    qr_content = _qr_content(provider)
    conn.execute(
        'INSERT INTO orders (id, user_id, provider, plan, amount_cents, status, qr_content, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (order_id, user_id, provider, PLAN['id'], PLAN['amount_cents'], 'pending', qr_content,
         now_iso()),
    )
    return order_payload({
        'id': order_id, 'provider': provider, 'plan': PLAN['id'],
        'amount_cents': PLAN['amount_cents'], 'status': 'pending',
        'qr_content': qr_content, 'created_at': now_iso(), 'paid_at': None,
    })


def order_payload(row) -> dict:
    data = dict(row) if isinstance(row, sqlite3.Row) else dict(row)
    return {
        'id': data['id'],
        'provider': data['provider'],
        'provider_label': PROVIDERS[data['provider']]['label'],
        'plan': data['plan'],
        'plan_label': PLAN['label'],
        'amount_cents': data['amount_cents'],
        'price_text': PLAN['price_text'],
        'status': data['status'],
        'qr_content': data['qr_content'],
        'qr_url': f'/api/pay/qr/{data["id"]}',
        'created_at': data['created_at'],
        'paid_at': data['paid_at'],
        'mock': PAY_MOCK,
    }


def get_order(conn: sqlite3.Connection, user_id: int, order_id: str) -> dict:
    row = conn.execute('SELECT * FROM orders WHERE id = ? AND user_id = ?',
                       (order_id, user_id)).fetchone()
    if not row:
        raise PayError('order_not_found', '订单不存在', 404)
    return order_payload(row)


def mark_paid(conn: sqlite3.Connection, user_id: int, order_id: str) -> dict:
    """标记订单已支付并开通会员；重复调用是幂等的。"""
    row = conn.execute('SELECT * FROM orders WHERE id = ? AND user_id = ?',
                       (order_id, user_id)).fetchone()
    if not row:
        raise PayError('order_not_found', '订单不存在', 404)
    if row['status'] == 'paid':
        return {'order': order_payload(row), **subscription_info(conn, user_id)}
    paid_at = now_iso()
    conn.execute('UPDATE orders SET status = ?, paid_at = ? WHERE id = ?',
                 ('paid', paid_at, order_id))
    _grant_subscription(conn, user_id, order_id)
    fresh = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    return {'order': order_payload(fresh), **subscription_info(conn, user_id)}


def qr_svg(content: str) -> bytes:
    image = qrcode.make(content, image_factory=SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    image.save(buf)
    return buf.getvalue()


def find_order_by_out_trade_no(conn: sqlite3.Connection, out_trade_no: str):
    return conn.execute('SELECT * FROM orders WHERE id = ?', (out_trade_no,)).fetchone()


def mark_order_paid_by_id(conn: sqlite3.Connection, order_id: str) -> dict | None:
    row = find_order_by_out_trade_no(conn, order_id)
    if not row:
        return None
    if row['status'] != 'paid':
        conn.execute('UPDATE orders SET status = ?, paid_at = ? WHERE id = ?',
                     ('paid', now_iso(), order_id))
        _grant_subscription(conn, row['user_id'], order_id)
    fresh = find_order_by_out_trade_no(conn, order_id)
    return order_payload(fresh)
