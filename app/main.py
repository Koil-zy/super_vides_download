"""FastAPI 入口：解析 / 下载(SSE 进度) / AI 总结(SSE 流式) / 静态页面。"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import ai, engine

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / 'static'

app = FastAPI(title='超级视频下载器', version='1.0.0')

SSE_HEADERS = {
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'X-Accel-Buffering': 'no',
}

IMAGE_HOSTS = (
    'hdslb.com', 'bilibili.com', 'ytimg.com', 'ggpht.com', 'douyinpic.com', 'douyin.com',
    'ixigua.com', 'kuaishou.com', 'weibo.com', 'zhimg.com', 'iqiyi.com', 'qq.com', 'youku.com',
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


def public_meta(meta: dict) -> dict:
    return {k: v for k, v in meta.items() if k != 'raw_info'}


class ParseIn(BaseModel):
    url: str


class DownloadIn(BaseModel):
    url: str
    format: str = 'bv*+ba/b'


class SummaryIn(BaseModel):
    url: str
    model: str | None = None


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
    url = body.url.strip()
    if not url.startswith(('http://', 'https://')):
        raise HTTPException(400, '请输入合法的视频链接（以 http:// 或 https:// 开头）')
    try:
        meta = await run_in_threadpool(engine.parse, url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f'解析失败：{clean_error(exc)}') from exc
    return public_meta(meta)


@app.post('/api/download')
async def download(body: DownloadIn):
    try:
        job = await run_in_threadpool(engine.start_download, body.url.strip(), body.format)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f'创建任务失败：{clean_error(exc)}') from exc
    return job


@app.get('/api/download/{job_id}/events')
async def download_events(job_id: str):
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
async def direct_download(url: str, format: str = 'bv*+ba/b'):
    """给浏览器直接下载用：后台起任务，等文件落盘后一次性返回附件。"""
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        raise HTTPException(400, '请输入合法的视频链接（以 http:// 或 https:// 开头）')
    try:
        job = await run_in_threadpool(engine.start_download, url, format)
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
async def list_jobs():
    return {'jobs': await run_in_threadpool(engine.jobs)}


@app.delete('/api/jobs/{job_id}')
async def remove_job(job_id: str):
    ok = await run_in_threadpool(engine.delete_job, job_id)
    if not ok:
        raise HTTPException(404, '任务不存在')
    return {'ok': True}


@app.get('/api/file/{job_id}')
async def get_file(job_id: str):
    path = engine.file_of(job_id)
    if not path:
        raise HTTPException(404, '文件不存在或尚未下载完成')
    return FileResponse(path, filename=path.name, media_type='application/octet-stream')


@app.post('/api/summarize')
async def summarize(body: SummaryIn):
    url = body.url.strip()

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


@app.get('/api/image')
async def image_proxy(url: str):
    host = urllib.parse.urlparse(url).hostname or ''
    if not any(host.endswith(h) or host == h for h in IMAGE_HOSTS):
        raise HTTPException(403, '不在允许的图片域名内')
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                'User-Agent': engine._UA,
                'Referer': 'https://www.bilibili.com/',
            })
            resp.raise_for_status()
        return Response(content=resp.content, media_type=resp.headers.get('content-type', 'image/jpeg'),
                        headers={'Cache-Control': 'public, max-age=86400'})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f'图片加载失败：{exc}') from exc


@app.get('/')
async def index():
    return FileResponse(STATIC_DIR / 'index.html')


app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')
