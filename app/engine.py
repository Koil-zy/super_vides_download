"""yt-dlp 二次开发封装层。

不重写下载逻辑，直接把 vendor/yt-dlp 源码作为库使用，只在其之上补一层
「解析结果裁剪 / 下载任务与进度 / 字幕转文本」的胶水代码。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / 'vendor' / 'yt-dlp'
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

import httpx  # noqa: E402
import yt_dlp  # noqa: E402

DOWNLOAD_DIR = ROOT / 'downloads'
DOWNLOAD_DIR.mkdir(exist_ok=True)

_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)


def _ffmpeg_location() -> str | None:
    """优先用 imageio-ffmpeg 自带二进制，其次系统 ffmpeg。"""
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    return shutil.which('ffmpeg')


def _base_opts(extra: dict | None = None) -> dict:
    opts: dict = {
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'noplaylist': True,
        'skip_download': True,
        'socket_timeout': 20,
        'retries': 3,
        'http_headers': {'User-Agent': _UA},
        'ffmpeg_location': _ffmpeg_location(),
    }
    cookies = os.getenv('YTDLP_COOKIES')
    if cookies and Path(cookies).exists():
        opts['cookiefile'] = cookies
    proxy = os.getenv('YTDLP_PROXY')
    if proxy:
        opts['proxy'] = proxy
    if extra:
        opts.update(extra)
    return opts


PARSE_CACHE: dict[str, dict] = {}
_CACHE_ORDER: list[str] = []
_CACHE_LOCK = threading.Lock()


def _remember(url: str, data: dict) -> dict:
    with _CACHE_LOCK:
        PARSE_CACHE[url] = data
        if url in _CACHE_ORDER:
            _CACHE_ORDER.remove(url)
        _CACHE_ORDER.append(url)
        while len(_CACHE_ORDER) > 40:
            PARSE_CACHE.pop(_CACHE_ORDER.pop(0), None)
    return data


def parse(url: str, use_cache: bool = True) -> dict:
    """解析链接，返回前端需要的精简元信息（不落库，内存缓存）。"""
    if use_cache and url in PARSE_CACHE:
        return PARSE_CACHE[url]

    with yt_dlp.YoutubeDL(_base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)

    if info.get('_type') == 'playlist' and info.get('entries'):
        entries = [e for e in info['entries'] if e]
        if entries:
            info = entries[0]

    return _remember(url, _shape(info))


def _duration_text(sec: float | None) -> str:
    if not sec:
        return ''
    sec = int(sec)
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f'{h:d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


def _size_text(size: int | float | None) -> str:
    if not size:
        return ''
    size = float(size)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.0f}{unit}' if unit in ('B', 'KB') else f'{size:.1f}{unit}'
        size /= 1024
    return ''


def _subtitles(info: dict) -> bool:
    return bool(info.get('subtitles') or info.get('automatic_captions'))


def _height_of(f: dict) -> int:
    """有些站点（如芒果TV）不给 height，只在 format_note 里写 480P/720P。"""
    height = f.get('height')
    if height:
        return int(height)
    note = f'{f.get("format_note") or ""} {f.get("resolution") or ""}'
    m = re.search(r'(\d{3,4})\s*[pP]', note)
    return int(m.group(1)) if m else 0


FREE_MAX_HEIGHT = 480
BEST_FORMAT = 'bv*+ba/b'
FREE_BEST_FORMAT = 'bv*[height<=480]+ba/b[height<=480]'


def _expr_height(fmt: str) -> int | None:
    """从 format 表达式里取出 height 上限，如 [height<=720]。"""
    caps = [int(x) for x in re.findall(r'height\s*<=\s*(\d+)', fmt)]
    if caps:
        return max(caps)
    caps = [int(x) for x in re.findall(r'height\s*<\s*(\d+)', fmt)]
    if caps:
        return max(caps) - 1
    return None


def format_height(info: dict, fmt: str) -> int:
    """估算某个 format 的最高清晰度；仅音频返回 0。"""
    height = _expr_height(fmt)
    if height:
        return height
    if fmt in ('ba', 'ba/b', 'ba*'):
        return 0
    if fmt in (BEST_FORMAT, 'bv*+ba', 'bv*', 'b'):
        heights = [_height_of(f) for f in info.get('formats') or [] if _height_of(f)]
        return max(heights) if heights else 2160
    for f in info.get('formats') or []:
        if f.get('format_id') == fmt:
            return _height_of(f)
    return 0


def resolve_format(url: str, fmt: str, premium: bool) -> tuple[str, int, bool]:
    """下载门禁：返回 (实际使用的 format, 该 format 的高度, 是否允许下载)。

    免费用户选「最高画质」时自动降到 480P 不弹窗；明确选更高档位则不允许，
    由上层返回 403 引导升级。
    """
    meta = parse(url)
    info = meta['raw_info']
    height = format_height(info, fmt)
    if premium or height <= FREE_MAX_HEIGHT:
        return fmt, height, True
    if fmt in (BEST_FORMAT, 'bv*+ba', 'bv*', 'b'):
        return FREE_BEST_FORMAT, FREE_MAX_HEIGHT, True
    return fmt, height, False


def _fmt_label(height: int) -> str:
    if height >= 2160:
        return '4K'
    if height >= 1440:
        return '2K'
    return f'{height}P'


def _format_options(info: dict) -> list[dict]:
    """把 yt-dlp 的 formats 收敛成人能点选的分档胶囊。"""
    best_video_by_h: dict[int, dict] = {}
    best_audio: dict | None = None

    for f in info.get('formats') or []:
        vcodec, acodec = f.get('vcodec'), f.get('acodec')
        has_video = vcodec and vcodec != 'none'
        has_audio = acodec and acodec != 'none'
        height = _height_of(f)
        if has_video and height:
            cur = best_video_by_h.get(height)
            if cur is None:
                best_video_by_h[height] = f
            else:
                # 优先免合并的单文件，其次 avc，最后比码率
                cur_merged = bool(cur.get('acodec') and cur.get('acodec') != 'none')
                if (has_audio and not cur_merged) or (has_audio == cur_merged and (f.get('tbr') or 0) > (cur.get('tbr') or 0)):
                    best_video_by_h[height] = f
        if not has_video and has_audio:
            if best_audio is None or (f.get('abr') or 0) > (best_audio.get('abr') or 0):
                best_audio = f

    options: list[dict] = [{'label': '最高画质', 'format': BEST_FORMAT, 'note': '自动选择最佳', 'size': None, 'height': None}]
    for height in sorted(best_video_by_h, reverse=True):
        f = best_video_by_h[height]
        merged = bool(f.get('acodec') and f.get('acodec') != 'none')
        size = f.get('filesize') or f.get('filesize_approx')
        if size and best_audio and not merged:
            size += best_audio.get('filesize') or best_audio.get('filesize_approx') or 0
        options.append({
            'label': _fmt_label(height),
            # 单文件直链直接用 format_id，避免 height 缺失时表达式选不中
            'format': f['format_id'] if merged and f.get('format_id') else f'bv*[height<={height}]+ba/b[height<={height}]',
            'note': (f.get('format_note') or '').strip() or ('单文件' if merged else ''),
            'size': _size_text(size),
            'height': height,
        })
    if best_audio:
        options.append({
            'label': '仅音频',
            'format': 'ba/b',
            'note': best_audio.get('ext') or 'm4a',
            'size': _size_text(best_audio.get('filesize') or best_audio.get('filesize_approx')),
            'height': 0,
        })
    # 无清晰度信息时，按码率列出前几个可下载单文件兜底
    if len(options) == 1:
        singles = sorted(
            [f for f in info.get('formats') or [] if f.get('url') and f.get('vcodec') not in (None, 'none')],
            key=lambda x: x.get('tbr') or 0, reverse=True)[:3]
        for f in singles:
            options.append({
                'label': (f.get('format_note') or f.get('resolution') or f.get('format_id') or '默认'),
                'format': f['format_id'] if f.get('format_id') else 'b',
                'note': '单文件',
                'size': _size_text(f.get('filesize') or f.get('filesize_approx')),
                'height': _height_of(f),
            })
        if len(options) == 1:
            options.append({'label': '最佳单文件', 'format': 'b', 'note': '无需合并', 'size': None, 'height': None})
    return options


def _shape(info: dict) -> dict:
    desc = (info.get('description') or '').strip()
    thumb = info.get('thumbnail')
    return {
        'url': info.get('webpage_url') or info.get('original_url'),
        'id': info.get('id'),
        'title': info.get('title') or '未命名视频',
        'thumbnail': thumb,
        'thumbnail_proxy': f'/api/image?url={httpx.QueryParams({"url": thumb})["url"]}' if thumb else None,
        'duration': info.get('duration'),
        'duration_text': _duration_text(info.get('duration')),
        'uploader': info.get('uploader') or info.get('channel') or '',
        'uploader_avatar': info.get('uploader_avatar'),
        'platform': info.get('extractor_key') or info.get('extractor') or '',
        'view_count': info.get('view_count'),
        'like_count': info.get('like_count'),
        'upload_date': info.get('upload_date') or '',
        'description': desc[:1500],
        'tags': (info.get('tags') or [])[:12],
        'has_subtitle': _subtitles(info),
        'formats': _format_options(info),
        'raw_info': info,
    }


# ---------------------------------------------------------------- 字幕 / 转录文本

LANG_PRIORITY = ('zh-Hans', 'zh-CN', 'zh', 'ai-zh', 'en', 'en-US', 'ja')


def _clean_caption(text: str) -> str:
    text = re.sub(r'^\ufeff?WEBVTT.*?\n\n', '', text, flags=re.S)
    text = re.sub(r'\d+\n\d{2}:\d{2}:\d{2}[,.]\d{3} --> \d{2}:\d{2}:\d{2}[,.]\d{3}', '', text)
    text = re.sub(r'<\d{2}:\d{2}:\d{2}[.,]\d{3}>', '', text)
    lines = []
    for raw in text.splitlines():
        line = re.sub(r'<[^>]+>', '', raw).strip()
        if not line or '-->' in line or line.isdigit() or line.upper().startswith('WEBVTT'):
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return re.sub(r'\s+', ' ', ' '.join(lines)).strip()


def _parse_caption_body(body: str, ext: str) -> str:
    if ext == 'json3' or body.lstrip().startswith('{'):
        try:
            data = httpx.Response(200, content=body.encode()).json()
            parts = []
            for ev in data.get('events') or []:
                for seg in ev.get('segs') or []:
                    parts.append(seg.get('utf8') or '')
            return re.sub(r'\s+', ' ', ''.join(parts)).strip()
        except Exception:
            pass
    return _clean_caption(body)


def _pick_subtitle(info: dict) -> tuple[str, dict] | tuple[None, None]:
    for source, group in (('subtitle', info.get('subtitles') or {}), ('auto', info.get('automatic_captions') or {})):
        by_lang = {str(k).lower(): k for k in group}
        for lang in LANG_PRIORITY:
            key = by_lang.get(lang.lower())
            if not key:
                continue
            tracks = group.get(key) or []
            tracks = sorted(tracks, key=lambda t: 0 if t.get('ext') in ('vtt', 'json3') else 1)
            if tracks:
                return source, {'lang': key, **tracks[0]}
    return None, None


def get_transcript(info: dict, max_chars: int = 6000) -> dict:
    """字幕优先；没有字幕就退回视频简介。返回 {text, source, lang}。"""
    source, track = _pick_subtitle(info)
    if track and track.get('url'):
        try:
            with httpx.Client(timeout=20, headers={'User-Agent': _UA}) as client:
                resp = client.get(track['url'])
                resp.raise_for_status()
                text = _parse_caption_body(resp.text, track.get('ext') or '')
            if len(text) > 60:
                return {'text': text[:max_chars], 'source': 'subtitle', 'lang': track.get('lang') or ''}
        except Exception:
            pass

    desc = (info.get('description') or '').strip()
    tags = '、'.join((info.get('tags') or [])[:8])
    fallback = '\n'.join(x for x in (desc, f'标签：{tags}' if tags else '') if x)
    return {'text': fallback[:max_chars], 'source': 'description', 'lang': ''}


# ---------------------------------------------------------------- 下载任务

_jobs: dict[str, dict] = {}
_jobs_order: list[str] = []
_jobs_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='ydl')

FORMAT_SAFE = re.compile(r'^[0-9a-zA-Z+*._<>=/\[\]():!-]{1,200}$')


def jobs(owner_id: int | None = None) -> list[dict]:
    """只返回指定用户任务（owner_id 为 None 时返回游客任务）。"""
    with _jobs_lock:
        out: list[dict] = []
        for jid in reversed(_jobs_order):
            rec = _jobs.get(jid)
            if rec and rec.get('owner_id') == owner_id:
                out.append(_public(rec))
                if len(out) >= 20:
                    break
        return out


def job(job_id: str) -> dict | None:
    with _jobs_lock:
        data = _jobs.get(job_id)
        return _public(data) if data else None


def owner_of(job_id: str) -> int | None:
    with _jobs_lock:
        rec = _jobs.get(job_id)
        return rec.get('owner_id') if rec else None


def _public(data: dict) -> dict:
    return {k: v for k, v in data.items() if k not in ('_ydl', 'owner_id')} | {
        'percent': round(data.get('percent') or 0, 1)}


def _update(job_id: str, **fields):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)
            _jobs[job_id]['updated_at'] = time.time()


def start_download(url: str, fmt: str, owner_id: int | None = None) -> dict:
    if not FORMAT_SAFE.match(fmt):
        raise ValueError('非法的 format 表达式')
    meta = parse(url)
    job_id = uuid.uuid4().hex[:10]
    rec = {
        'id': job_id,
        'url': url,
        'format': fmt,
        'owner_id': owner_id,
        'title': meta['title'],
        'thumbnail': meta.get('thumbnail_proxy'),
        'duration_text': meta.get('duration_text') or '',
        'platform': meta.get('platform') or '',
        'uploader': meta.get('uploader') or '',
        'status': 'queued',
        'stage': '排队中',
        'percent': 0.0,
        'speed': '',
        'eta': '',
        'total_text': '',
        'downloaded_text': '',
        'filepath': None,
        'filename': None,
        'error': None,
        'created_at': time.time(),
        'updated_at': time.time(),
    }
    with _jobs_lock:
        _jobs[job_id] = rec
        _jobs_order.append(job_id)
        while len(_jobs_order) > 60:
            _jobs.pop(_jobs_order.pop(0), None)
    _pool.submit(_run_download, job_id)
    return _public(rec)


def _human(n: float | None) -> str:
    if not n:
        return ''
    return _size_text(n)


def _run_download(job_id: str):
    url = _jobs[job_id]['url']
    fmt = _jobs[job_id]['format']

    def hook(d: dict):
        if d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            done = d.get('downloaded_bytes') or 0
            pct = (done / total * 100) if total else (d.get('_percent_str') or '0%').strip().rstrip('%') or 0
            try:
                pct = float(str(pct).strip().rstrip('%'))
            except ValueError:
                pct = 0.0
            speed = d.get('speed')
            eta = d.get('eta')
            _update(
                job_id,
                status='downloading',
                stage='下载中',
                percent=max(0.0, min(99.0, pct)),
                speed=f'{_human(speed)}/s' if speed else '',
                eta=f'{int(eta)}s' if eta else '',
                total_text=_human(total),
                downloaded_text=_human(done),
            )
        elif d.get('status') == 'finished':
            _update(job_id, percent=99.0, stage='合并/后处理中', speed='', eta='')

    def pp_hook(d: dict):
        if d.get('status') == 'started':
            _update(job_id, stage=f'后处理：{d.get("postprocessor") or ""}'.strip('：'))

    opts = _base_opts({
        'skip_download': False,
        'format': fmt,
        'outtmpl': {'default': '%(title).80s [%(id)s].%(ext)s'},
        'paths': {'home': str(DOWNLOAD_DIR)},
        'progress_hooks': [hook],
        'postprocessor_hooks': [pp_hook],
        'windowsfilenames': True,
        'overwrites': False,
        'concurrent_fragment_downloads': 4,
    })
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        reqs = (info or {}).get('requested_downloads') or []
        path = None
        for r in reqs:
            path = r.get('filepath') or r.get('_filename')
            if path and Path(path).exists():
                break
        if not path or not Path(path).exists():
            candidates = sorted(DOWNLOAD_DIR.glob(f'*[{info.get("id")}]*'), key=lambda p: p.stat().st_mtime)
            path = str(candidates[-1]) if candidates else None
        _update(
            job_id,
            status='done',
            stage='已完成',
            percent=100.0,
            filepath=path,
            filename=Path(path).name if path else None,
        )
    except Exception as exc:  # noqa: BLE001
        _update(job_id, status='error', stage='失败', error=str(exc)[:300])


def delete_job(job_id: str) -> bool:
    with _jobs_lock:
        rec = _jobs.pop(job_id, None)
        if job_id in _jobs_order:
            _jobs_order.remove(job_id)
    if not rec:
        return False
    path = rec.get('filepath')
    if path:
        p = Path(path).resolve()
        if DOWNLOAD_DIR.resolve() in p.parents and p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    return True


def file_of(job_id: str) -> Path | None:
    rec = _jobs.get(job_id) or {}
    path = rec.get('filepath')
    if path and Path(path).exists():
        return Path(path)
    return None
