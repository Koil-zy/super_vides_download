"""AI 视频总结：调用本地 Ollama，逐 token 产出（上层用 SSE 推给前端）。"""

from __future__ import annotations

import json
import os
from typing import AsyncIterator

import httpx

OLLAMA_BASE = os.getenv('OLLAMA_BASE', 'http://127.0.0.1:11434').rstrip('/')
DEFAULT_MODEL = os.getenv('OLLAMA_MODEL', 'deepseek-r1:1.5b')

SYSTEM_PROMPT = (
    '你是视频内容分析助手。请只依据用户提供的素材做总结，不要编造未出现的事实；'
    '素材不足时明确说明。输出使用简体中文，纯文本，不用 Markdown 标题符号，'
    '总字数控制在 200 字左右（含标点，不得超过 250 字），结构固定为：\n'
    '一句话概括\n核心要点（3-5 条，每条以“- ”开头）\n'
    '信息密度与可信度（一句话，说明总结依据是字幕还是简介）'
)


def _prompt(meta: dict, transcript: dict) -> str:
    source_name = '视频字幕' if transcript['source'] == 'subtitle' else '视频简介/标签（无字幕）'
    tags = '、'.join(meta.get('tags') or []) or '无'
    return (
        f'视频标题：{meta.get("title")}\n'
        f'平台：{meta.get("platform")}｜UP主：{meta.get("uploader")}｜时长：{meta.get("duration_text")}\n'
        f'标签：{tags}\n'
        f'素材来源：{source_name}\n'
        f'素材正文：\n{transcript["text"] or "（无可用素材）"}'
    )


async def list_models() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f'{OLLAMA_BASE}/api/tags')
            resp.raise_for_status()
            data = resp.json()
        names = [m['name'] for m in data.get('models') or [] if m.get('name')]
        model = DEFAULT_MODEL if DEFAULT_MODEL in names else (names[0] if names else DEFAULT_MODEL)
        return {'online': True, 'models': names, 'model': model}
    except Exception as exc:  # noqa: BLE001
        return {'online': False, 'models': [], 'model': DEFAULT_MODEL, 'error': str(exc)[:200]}


async def stream_summary(meta: dict, transcript: dict, model: str | None = None) -> AsyncIterator[dict]:
    payload = {
        'model': model or DEFAULT_MODEL,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': _prompt(meta, transcript)},
        ],
        'stream': True,
        # deepseek-r1 的思考过程也计入 num_predict，预留足够 token 保证正文完整
        'options': {'temperature': 0.3, 'num_ctx': 8192, 'num_predict': 1024},
    }
    timeout = httpx.Timeout(connect=5, read=180, write=20, pool=None)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream('POST', f'{OLLAMA_BASE}/api/chat', json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode('utf-8', 'ignore')[:300]
                    yield {'type': 'error', 'message': f'Ollama 返回 {resp.status_code}：{body}'}
                    return
                in_think = False
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get('error'):
                        yield {'type': 'error', 'message': str(chunk['error'])[:300]}
                        return
                    piece = (chunk.get('message') or {}).get('content') or ''
                    # 推理模型可能把思考过程包在  thinking 里，流式过滤掉
                    if in_think:
                        if '<｜end▁of▁thinking｜>' in piece:
                            in_think = False
                            piece = piece.split('<｜end▁of▁thinking｜>', 1)[1]
                        else:
                            piece = ''
                    if ' thinking' in piece:
                        in_think = '<｜end▁of▁thinking｜>' not in piece
                        piece = piece.split(' thinking', 1)[0]
                    if piece:
                        yield {'type': 'delta', 'text': piece}
                    if chunk.get('done'):
                        yield {'type': 'done'}
                        return
        yield {'type': 'done'}
    except httpx.ConnectError:
        yield {'type': 'error', 'message': f'无法连接本地 Ollama（{OLLAMA_BASE}），请先启动 ollama serve'}
    except Exception as exc:  # noqa: BLE001
        yield {'type': 'error', 'message': f'总结失败：{str(exc)[:200]}'}
