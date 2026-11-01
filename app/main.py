"""FastAPI 入口：解析 / 下载(SSE 进度) / AI 总结(SSE 流式) / 认证 / 支付 / 静态页面。"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import ai, auth, engine, pay
from .db import get_db, init_db

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / 'static'

app = FastAPI(title='超级视频下载器', version='1.0.0')
init_db()

SSE_HEADERS = {
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'X-Accel-Buffering': 'no',
}

IMAGE_HOSTS = (
    'hdslb.com', 'bilibili.com', 'ytimg.com', 'ggpht.com', 'douyinpic.com', 'douyin.com',
    'ixigua.com', 'kuaishou.com', 'weibo.com', 'zhimg.com', 'iqiyi.com', 'iqiyipic.com',
    'qq.com', 'youku.com',
    'tiktokcdn.com', 'twimg.com', 'pinimg.com', 'sinaimg.cn', 'bcebos.com', 'byteimg.com',
    'acfun.cn', 'xiaohongshu.com', 'xhscdn.com', 'snssdk.com', 'amemv.com', 'vimeo.com',
    'dailymotion.com', 'nicovideo.jp', 'sohu.com', 'mgtv.com', 'hitv.com', 'le.com', 'pptv.com',
    'googleusercontent.com', 'sndcdn.com', 'redd.it', 'twitch.tv', 'streamscharts.com',
)


def sse(payload: dict) -> str:
    return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'


def clean_error(exc: Exception) -> str:
    msg = re.sub(r'\x1b\[[0-9;]*m', '', str(exc)).replace('ERROR: ', '').strip()
    low = msg.lower()
    if 'not available from your location' in low or 'geo restrict' in low or 'copyright' in low:
        return '该视频受版权/地域限制，当前网络无法播放；可配置代理或更换网络后重试'
    if 'no video formats found' in low:
        return '未找到可下载的视频源：可能是会员专享、需要登录 Cookie，或受地域限制'
    if 'unsupported url' in low:
        return '暂不支持该链接'
    if 'http error 404' in low:
        return '视频不存在或已下架（接口返回 404）'
    return msg[:400] or exc.__class__.__name__


def api_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {'code': code, 'message': message})


def clean_url(raw: str) -> str:
    url = (raw or '').strip()
    if not url.startswith(('http://', 'https://')):
        raise HTTPException(400, '请输入合法的视频链接（以 http:// 或 https:// 开头）')
    return url


def public_meta(meta: dict) -> dict:
    return {k: v for k, v in meta.items() if k != 'raw_info'}


def require_login(request: Request) -> dict:
    try:
        return auth.require_user(request)
    except auth.AuthError as exc:
        raise api_error(exc.status, exc.code, exc.message) from exc


def is_premium(user_id: int) -> bool:
    with get_db() as conn:
        return pay.subscription_info(conn, user_id)['premium']


async def gated_format(request: Request, url: str, fmt: str) -> tuple[dict, str, int, bool]:
    """下载门禁：登录校验 + 清晰度校验，返回 (用户, 实际 format, 高度, 是否降级)。"""
    user = require_login(request)
    premium = await run_in_threadpool(is_premium, user['id'])
    try:
        effective, height, allowed = await run_in_threadpool(
            engine.resolve_format, url, fmt, premium)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f'解析失败：{clean_error(exc)}') from exc
    if not allowed:
        raise HTTPException(403, {
            'code': 'subscription_required',
            'message': f'{height}P 属于会员清晰度，开通月度会员后即可下载',
            'height': height,
            'plan': pay.PLAN,
            'providers': pay.PROVIDERS,
        })
    return user, effective, height, effective != fmt


class ParseIn(BaseModel):
    url: str


class DownloadIn(BaseModel):
    url: str
    format: str = engine.BEST_FORMAT


class SummaryIn(BaseModel):
    url: str
    model: str | None = None


class RegisterIn(BaseModel):
    email: str
    password: str
    nickname: str | None = None


class LoginIn(BaseModel):
    email: str
    password: str


class PayOrderIn(BaseModel):
    provider: str


@app.get('/api/health')
async def health():
    models = await ai.list_models()
    staged = engine.VENDOR / 'yt_dlp' / 'version.py'
    version = ''
    if staged.exists():
        found = re.search(r"__version__ = '([^']+)'", staged.read_text(encoding='utf-8'))
        version = found.group(1) if found else ''
    return {
        'ok': True,
        'engine': f'yt-dlp {version} (vendored)',
        'ffmpeg': engine._ffmpeg_location(),
        'ollama': models,
    }


@app.post('/api/parse')
async def parse(body: ParseIn):
    url = clean_url(body.url)
    try:
        meta = await run_in_threadpool(engine.parse, url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f'解析失败：{clean_error(exc)}') from exc
    return public_meta(meta)


# ------------------------------------------------------------------ 认证


@app.post('/api/auth/register')
async def auth_register(body: RegisterIn):
    try:
        with get_db() as conn:
            user = auth.register(conn, body.email, body.password, body.nickname)
            token, _ = auth.create_session(conn, user['id'])
            sub = pay.subscription_info(conn, user['id'])
    except auth.AuthError as exc:
        raise api_error(exc.status, exc.code, exc.message) from exc
    resp = JSONResponse({'user': user, 'code': 'ok', **sub})
    auth.set_session_cookie(resp, token)
    return resp


@app.post('/api/auth/login')
async def auth_login(body: LoginIn):
    try:
        with get_db() as conn:
            user = auth.login(conn, body.email, body.password)
            token, _ = auth.create_session(conn, user['id'])
            sub = pay.subscription_info(conn, user['id'])
    except auth.AuthError as exc:
        raise api_error(exc.status, exc.code, exc.message) from exc
    resp = JSONResponse({'user': user, 'code': 'ok', **sub})
    auth.set_session_cookie(resp, token)
    return resp


@app.post('/api/auth/logout')
async def auth_logout(request: Request):
    token = request.cookies.get(auth.SESSION_COOKIE)
    with get_db() as conn:
        auth.drop_session(conn, token)
    resp = JSONResponse({'ok': True})
    auth.clear_session_cookie(resp)
    return resp


@app.get('/api/auth/me')
async def auth_me(request: Request):
    user = auth.current_user(request)
    if not user:
        return {'user': None, 'premium': False, 'subscription': None,
                'plan': pay.PLAN, 'providers': pay.PROVIDERS, 'mock_pay': pay.PAY_MOCK}
    with get_db() as conn:
        sub = pay.subscription_info(conn, user['id'])
    return {'user': user, **sub, 'plan': pay.PLAN, 'providers': pay.PROVIDERS,
            'mock_pay': pay.PAY_MOCK}


# ------------------------------------------------------------------ 支付


@app.post('/api/pay/orders')
async def pay_create_order(body: PayOrderIn, request: Request):
    user = require_login(request)
    try:
        with get_db() as conn:
            order = pay.create_order(conn, user['id'], body.provider)
    except pay.PayError as exc:
        raise api_error(exc.status, exc.code, exc.message) from exc
    return {'order': order, 'plan': pay.PLAN, 'providers': pay.PROVIDERS}


@app.get('/api/pay/orders/{order_id}')
async def pay_order_status(order_id: str, request: Request):
    user = require_login(request)
    try:
        with get_db() as conn:
            order = pay.get_order(conn, user['id'], order_id)
            sub = pay.subscription_info(conn, user['id'])
    except pay.PayError as exc:
        raise api_error(exc.status, exc.code, exc.message) from exc
    return {'order': order, **sub}


@app.get('/api/pay/qr/{order_id}')
async def pay_qr(order_id: str, request: Request):
    user = require_login(request)
    try:
        with get_db() as conn:
            order = pay.get_order(conn, user['id'], order_id)
        svg = await run_in_threadpool(pay.qr_svg, order['qr_content'])
    except pay.PayError as exc:
        raise api_error(exc.status, exc.code, exc.message) from exc
    return Response(content=svg, media_type='image/svg+xml',
                    headers={'Cache-Control': 'no-store'})


@app.post('/api/pay/mock/{order_id}')
async def pay_mock(order_id: str, request: Request):
    """测试用：直接标记支付成功（PAY_MOCK=0 时关闭）。"""
    if not pay.PAY_MOCK:
        raise api_error(403, 'mock_disabled', '当前环境已关闭模拟支付')
    user = require_login(request)
    try:
        with get_db() as conn:
            result = pay.mark_paid(conn, user['id'], order_id)
    except pay.PayError as exc:
        raise api_error(exc.status, exc.code, exc.message) from exc
    return result


@app.post('/api/pay/notify/{provider}')
async def pay_notify(provider: str, request: Request):
    """支付异步回调占位：真实接入需在此校验签名与金额。"""
    if provider not in pay.PROVIDERS:
        raise api_error(404, 'unknown_provider', '未知支付渠道')
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    if not pay.PAY_MOCK:
        # TODO 正式接入：微信 APIv3 验签 / 支付宝 RSA2 验签，校验 amount 后调用 mark_paid
        raise api_error(501, 'not_implemented', '真实支付回调尚未接入，请先配置渠道密钥并实现验签')
    order_id = str(payload.get('out_trade_no') or payload.get('order_id') or '')
    if order_id:
        with get_db() as conn:
            pay.mark_order_paid_by_id(conn, order_id)
    return {'code': 'SUCCESS', 'message': 'OK'}


# ------------------------------------------------------------------ 下载（含门禁）


@app.post('/api/download/check')
async def download_check(body: DownloadIn, request: Request):
    """下载前置校验：前端据此决定弹登录窗 / 升级窗，或拿到降级后的 format。"""
    url = clean_url(body.url)
    user, effective, height, downgraded = await gated_format(request, url, body.format)
    return {
        'ok': True,
        'user_id': user['id'],
        'format': effective,
        'height': height,
        'downgraded': downgraded,
        'free_max_height': engine.FREE_MAX_HEIGHT,
    }


@app.post('/api/download')
async def download(body: DownloadIn, request: Request):
    url = clean_url(body.url)
    user, effective, height, downgraded = await gated_format(request, url, body.format)
    try:
        job = await run_in_threadpool(engine.start_download, url, effective, user['id'])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f'创建任务失败：{clean_error(exc)}') from exc
    return {**job, 'format': effective, 'height': height, 'downgraded': downgraded}


@app.get('/api/download/{job_id}/events')
async def download_events(job_id: str, request: Request):
    user = auth.current_user(request)
    if engine.owner_of(job_id) != (user['id'] if user else None):
        raise api_error(404, 'job_not_found', '任务不存在或已过期')

    async def gen():
        last = None
        while True:
            rec = engine.job(job_id)
            if not rec:
                yield sse({'type': 'error', 'message': '任务不存在或已过期'})
                return
            snapshot = {k: rec[k] for k in ('status', 'stage', 'percent', 'speed', 'eta', 'total_text', 'downloaded_text')}
            if snapshot != last:
                yield sse({'type': 'progress', **rec})
                last = snapshot
            if rec['status'] in ('done', 'error'):
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type='text/event-stream', headers=SSE_HEADERS)


@app.get('/api/dl')
async def direct_download(request: Request, url: str, format: str = engine.BEST_FORMAT):
    """给浏览器直接下载用：后台起任务，等文件落盘后一次性返回附件。"""
    url = clean_url(url)
    user, effective, _height, _downgraded = await gated_format(request, url, format)
    try:
        job = await run_in_threadpool(engine.start_download, url, effective, user['id'])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f'创建任务失败：{clean_error(exc)}') from exc

    for _ in range(7200):  # 最长等待约 1 小时
        rec = engine.job(job['id'])
        if not rec:
            raise HTTPException(404, '任务不存在或已过期')
        if rec['status'] == 'done':
            break
        if rec['status'] == 'error':
            raise HTTPException(400, f'下载失败：{rec.get("error") or "未知错误"}')
        await asyncio.sleep(0.5)
    else:
        raise HTTPException(504, '下载超时，请稍后重试')

    path = engine.file_of(job['id'])
    if not path:
        raise HTTPException(404, '文件不存在或尚未下载完成')
    return FileResponse(path, filename=path.name, media_type='application/octet-stream')


@app.get('/api/jobs')
async def list_jobs(request: Request):
    user = auth.current_user(request)
    return {'jobs': await run_in_threadpool(engine.jobs, user['id'] if user else None)}


@app.delete('/api/jobs/{job_id}')
async def remove_job(job_id: str, request: Request):
    user = auth.current_user(request)
    if engine.owner_of(job_id) != (user['id'] if user else None):
        raise api_error(404, 'job_not_found', '任务不存在')
    ok = await run_in_threadpool(engine.delete_job, job_id)
    if not ok:
        raise HTTPException(404, '任务不存在')
    return {'ok': True}


@app.get('/api/file/{job_id}')
async def get_file(job_id: str, request: Request):
    user = auth.current_user(request)
    if engine.owner_of(job_id) != (user['id'] if user else None):
        raise api_error(404, 'job_not_found', '文件不存在或尚未下载完成')
    path = engine.file_of(job_id)
    if not path:
        raise HTTPException(404, '文件不存在或尚未下载完成')
    return FileResponse(path, filename=path.name, media_type='application/octet-stream')


# ------------------------------------------------------------------ AI 总结（保持匿名可用）


@app.post('/api/summarize')
async def summarize(body: SummaryIn):
    url = clean_url(body.url)

    async def gen():
        try:
            meta = await run_in_threadpool(engine.parse, url)
            transcript = await run_in_threadpool(engine.get_transcript, meta['raw_info'])
        except Exception as exc:  # noqa: BLE001
            yield sse({'type': 'error', 'message': f'获取素材失败：{clean_error(exc)}'})
            return

        model = body.model or (await ai.list_models()).get('model') or ai.DEFAULT_MODEL
        yield sse({
            'type': 'meta',
            'source': transcript['source'],
            'lang': transcript['lang'],
            'chars': len(transcript['text']),
            'model': model,
        })
        async for chunk in ai.stream_summary(public_meta(meta), transcript, model):
            yield sse(chunk)

    return StreamingResponse(gen(), media_type='text/event-stream', headers=SSE_HEADERS)


@app.get('/api/ollama/models')
async def ollama_models():
    return await ai.list_models()


def image_referer(host: str) -> str:
    """部分图床会校验 Referer，按域名给对应的来源页。"""
    if host.endswith(('iqiyipic.com', 'iqiyi.com')):
        return 'https://www.iqiyi.com/'
    if host.endswith(('hitv.com', 'mgtv.com')):
        return 'https://www.mgtv.com/'
    if host.endswith(('hdslb.com', 'bilibili.com')):
        return 'https://www.bilibili.com/'
    return ''


@app.get('/api/image')
async def image_proxy(url: str):
    host = urllib.parse.urlparse(url).hostname or ''
    if not any(host.endswith(h) or host == h for h in IMAGE_HOSTS):
        raise HTTPException(403, '不在允许的图片域名内')
    headers = {'User-Agent': engine._UA}
    referer = image_referer(host)
    if referer:
        headers['Referer'] = referer
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
        return Response(content=resp.content, media_type=resp.headers.get('content-type', 'image/jpeg'),
                        headers={'Cache-Control': 'public, max-age=86400'})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f'图片加载失败：{exc}') from exc


@app.get('/')
async def index():
    return FileResponse(STATIC_DIR / 'index.html')


app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')
