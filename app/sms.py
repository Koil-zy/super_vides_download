"""管理端短信验证码：中国移动短信通道（当前为伪造实现）+ Redis 缓存。

验证码存放在 Redis（默认 redis://127.0.0.1:6379/0），5 分钟有效，60 秒内不可重发；
Redis 不可用时会自动退回进程内内存缓存，保证本地演示不中断。
真实接入中国移动通道时，只需要替换本文件顶部的 CMCC 常量并实现 _send_via_cmcc。
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import threading
import time

import redis

# ------------------------------------------------------------------ 配置
REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')
SMS_MOCK = os.getenv('SMS_MOCK', '1') != '0'
CODE_TTL = 300          # 验证码有效期（秒）
RESEND_INTERVAL = 60    # 重发间隔（秒）
CODE_LENGTH = 6

# 中国移动短信通道（云 MAS）—— 伪造占位，正式接入时替换
CMCC_API_URL = os.getenv('CMCC_API_URL', 'https://mock-cmcc.example.com/sms/send')
CMCC_ACCOUNT = os.getenv('CMCC_ACCOUNT', 'mock_cmcc_account')
CMCC_PASSWORD = os.getenv('CMCC_PASSWORD', 'mock_cmcc_password')
CMCC_SIGN = os.getenv('CMCC_SIGN', '超级视频')

_PHONE_RE = re.compile(r'^1[3-9]\d{9}$')

# 本机 Redis 为 5.x（不支持 RESP3 的 HELLO），必须固定 protocol=2
_client: redis.Redis | None = None
_MEMORY: dict[str, tuple[str, float]] = {}
_LOCK = threading.Lock()


class SmsError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _redis_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            REDIS_URL, protocol=2, decode_responses=True,
            socket_connect_timeout=2, socket_timeout=2,
        )
    return _client


def _cache_set(key: str, value: str, ttl: int) -> None:
    try:
        _redis_client().set(key, value, ex=ttl)
        return
    except Exception:  # noqa: BLE001  Redis 不可用 → 内存兜底
        pass
    with _LOCK:
        _MEMORY[key] = (value, time.time() + ttl)


def _cache_set_once(key: str, value: str, ttl: int) -> bool:
    """SET NX：返回 True 表示抢到（用于重发限频）。"""
    try:
        return bool(_redis_client().set(key, value, ex=ttl, nx=True))
    except Exception:  # noqa: BLE001
        pass
    with _LOCK:
        expired = _MEMORY.get(key)
        if expired and expired[1] > time.time():
            return False
        _MEMORY[key] = (value, time.time() + ttl)
        return True


def _cache_get(key: str) -> str | None:
    try:
        return _redis_client().get(key)
    except Exception:  # noqa: BLE001
        pass
    with _LOCK:
        item = _MEMORY.get(key)
        if not item:
            return None
        if item[1] <= time.time():
            _MEMORY.pop(key, None)
            return None
        return item[0]


def _cache_del(key: str) -> None:
    try:
        _redis_client().delete(key)
    except Exception:  # noqa: BLE001
        pass
    with _LOCK:
        _MEMORY.pop(key, None)


def _check_phone(phone: str) -> str:
    phone = (phone or '').strip()
    if not _PHONE_RE.match(phone):
        raise SmsError('invalid_phone', '请输入正确的 11 位手机号')
    return phone


def _code_key(scene: str, phone: str) -> str:
    return f'sv:sms:code:{scene}:{phone}'


def _limit_key(scene: str, phone: str) -> str:
    return f'sv:sms:limit:{scene}:{phone}'


def send_code(phone: str, scene: str = 'register') -> dict:
    """发送验证码；SMS_MOCK=1 时直接返回 dev_code 方便测试。"""
    phone = _check_phone(phone)
    if scene not in ('register', 'login'):
        raise SmsError('invalid_scene', '未知的验证码场景')
    if not _cache_set_once(_limit_key(scene, phone), '1', RESEND_INTERVAL):
        raise SmsError('code_too_frequent', '验证码已发送，请 60 秒后再试', 429)

    code = f'{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}'
    if not SMS_MOCK:
        try:
            _send_via_cmcc(phone, code)
        except SmsError:
            _cache_del(_limit_key(scene, phone))
            raise
    _cache_set(_code_key(scene, phone), code, CODE_TTL)
    return {'mock': SMS_MOCK, 'dev_code': code if SMS_MOCK else '', 'ttl': CODE_TTL}


def verify_code(phone: str, scene: str, code: str) -> None:
    phone = _check_phone(phone)
    code = (code or '').strip()
    if not re.fullmatch(rf'\d{{{CODE_LENGTH}}}', code):
        raise SmsError('invalid_code', '请输入 6 位数字验证码')
    stored = _cache_get(_code_key(scene, phone))
    if not stored:
        raise SmsError('code_expired', '验证码已过期，请重新获取')
    if not hmac.compare_digest(stored, code):
        raise SmsError('code_invalid', '验证码不正确')
    _cache_del(_code_key(scene, phone))


def _send_via_cmcc(phone: str, code: str) -> None:
    """调用中国移动短信接口（占位）。正式接入时在此处发送 HTTP 请求。"""
    raise SmsError(
        'sms_not_configured',
        '中国移动短信通道尚未配置：请填写 CMCC_API_URL / CMCC_ACCOUNT / CMCC_PASSWORD，'
        '或在测试环境保持 SMS_MOCK=1',
        501,
    )
