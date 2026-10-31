(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    meta: null,
    format: null,
    jobs: [],
    jobsRaw: '',
    page: 0,
    summarizing: false,
  };

  const PAGE_SIZE = 12;

  /* ------------------------------------------------------------ 小工具 */
  let toastTimer = 0;
  function toast(msg) {
    const el = $('toast');
    el.textContent = msg;
    el.classList.add('is-show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('is-show'), 2600);
  }

  function fmtCount(n) {
    if (!n && n !== 0) return '';
    if (n >= 100000000) return (n / 100000000).toFixed(1).replace('.0', '') + '亿';
    if (n >= 10000) return (n / 10000).toFixed(1).replace('.0', '') + '万';
    return String(n);
  }

  function fmtDate(s) {
    if (!s || s.length !== 8) return '';
    return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function api(path, options) {
    const resp = await fetch(path, options);
    if (!resp.ok) {
      let detail = `请求失败（${resp.status}）`;
      try {
        const data = await resp.json();
        if (data && data.detail) detail = data.detail;
      } catch (_) { /* ignore */ }
      throw new Error(detail);
    }
    return resp.json();
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /* ------------------------------------------------------------ 状态检测 */
  async function loadModels() {
    const dot = $('ollamaDot');
    try {
      const data = await api('/api/ollama/models');
      dot.classList.toggle('is-online', !!data.online);
      dot.classList.toggle('is-offline', !data.online);
      dot.title = data.online ? '本地 Ollama 已启动，可用于 AI 总结' : '本地 Ollama 未启动，AI 总结不可用';
    } catch (err) {
      dot.classList.remove('is-online');
      dot.classList.add('is-offline');
      dot.title = '本地 Ollama 未启动，AI 总结不可用';
    }
  }

  /* ------------------------------------------------------------ 解析 */
  async function doParse(rawUrl) {
    const url = (rawUrl || '').trim();
    if (!url) { toast('先粘贴一个视频链接'); return; }
    const hint = $('resultHint');
    hint.textContent = '解析中…';
    $('resultEmpty').querySelector('p').textContent = '正在解析视频信息，首次可能需要几秒…';
    try {
      const meta = await api('/api/parse', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
      state.meta = meta;
      state.format = (meta.formats.find((f) => f.format === 'bv*+ba/b') || meta.formats[0] || {}).format || null;
      renderResult(meta);
      resetSummary();
      hint.textContent = `已解析 · ${meta.platform || '未知平台'}`;
    } catch (err) {
      hint.textContent = '解析失败';
      $('resultEmpty').querySelector('p').textContent = '在顶部搜索框粘贴链接，回车即可解析出清晰度与下载地址';
      toast(err.message);
    }
  }

  function renderResult(meta) {
    $('resultEmpty').hidden = true;
    $('result').hidden = false;

    const img = $('coverImg');
    img.src = meta.thumbnail_proxy || meta.thumbnail || '';
    img.onerror = () => { img.style.display = 'none'; };
    img.style.display = '';
    $('coverDuration').textContent = meta.duration_text || '';

    $('videoTitle').textContent = meta.title;

    const row = $('videoMeta');
    row.innerHTML = '';
    if (meta.platform) row.appendChild(el('span', 'tag tag--blue', meta.platform));
    if (meta.uploader) row.appendChild(el('span', 'up', 'UP主 · ' + meta.uploader));
    if (meta.view_count) row.appendChild(el('span', '', '播放 ' + fmtCount(meta.view_count)));
    if (meta.like_count) row.appendChild(el('span', '', '点赞 ' + fmtCount(meta.like_count)));
    if (meta.upload_date) row.appendChild(el('span', '', fmtDate(meta.upload_date)));
    row.appendChild(el('span', 'tag ' + (meta.has_subtitle ? 'tag--pink' : ''), meta.has_subtitle ? '有字幕' : '无字幕'));

    renderFormats(meta.formats || []);

    $('downloadStatus').textContent = '';
    $('downloadBtn').disabled = false;
    $('downloadBtn').textContent = '下载视频';
    $('summaryBtn').disabled = false;
    $('aiStartBtn').disabled = false;
    $('aiStartBtn').textContent = '生成总结';

    const descBox = $('descBox');
    if (meta.description) {
      descBox.hidden = false;
      $('descText').textContent = meta.description;
    } else {
      descBox.hidden = true;
    }
  }

  function renderFormats(formats) {
    const list = $('formatList');
    list.innerHTML = '';
    formats.forEach((f) => {
      const btn = el('button', 'fmt' + (f.format === state.format ? ' is-active' : ''));
      btn.type = 'button';
      btn.dataset.format = f.format;
      btn.appendChild(el('span', '', f.label));
      btn.appendChild(el('small', '', f.size || f.note || ''));
      btn.addEventListener('click', () => {
        state.format = f.format;
        list.querySelectorAll('.fmt').forEach((n) => n.classList.toggle('is-active', n === btn));
      });
      list.appendChild(btn);
    });
  }

  /* ------------------------------------------------------------ 下载 */
  // 与「最近下载」里的「重新下载」等价：点击即由浏览器下载文件。
  // 服务端先把 m3u8 合并成 mp4，就绪后浏览器开始保存。
  function startDownload() {
    if (!state.meta) { toast('先解析一个视频'); return; }
    const fmt = state.format || 'bv*+ba/b';
    const href = `/api/dl?url=${encodeURIComponent(state.meta.url)}&format=${encodeURIComponent(fmt)}`;
    $('downloadStatus').textContent = '正在准备文件…';
    // 在用户点击事件内触发，避免被浏览器的“自动下载”拦截
    const a = document.createElement('a');
    a.href = href;
    a.download = '';
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast('文件准备完成后浏览器会自动开始下载');
    trackDownload(state.meta.url, fmt);
  }

  // 浏览器的下载动作无法被脚本感知，这里轮询服务端任务状态来更新提示
  async function trackDownload(url, fmt) {
    const token = (state.track = (state.track || 0) + 1);
    const startedAt = Date.now();
    const statusEl = $('downloadStatus');
    while (Date.now() - startedAt < 60 * 60 * 1000) {
      await sleep(1500);
      if (state.track !== token) return;
      let hit;
      try {
        const data = await api('/api/jobs');
        hit = (data.jobs || []).find((j) => j.url === url && j.format === fmt
          && (j.created_at || 0) * 1000 >= startedAt - 2000);
      } catch (_) {
        continue;
      }
      if (!hit) continue;
      if (hit.status === 'done') {
        statusEl.textContent = '下载完成 · 已保存到本机';
        toast('下载完成');
        refreshJobs();
        return;
      }
      if (hit.status === 'error') {
        statusEl.textContent = '下载失败';
        toast(hit.error || '下载失败，请重试');
        return;
      }
      statusEl.textContent = hit.percent ? `正在下载 ${Math.round(hit.percent)}%` : '正在准备文件…';
    }
  }

  /* ------------------------------------------------------------ AI 总结 */
  function resetSummary() {
    state.summarizing = false;
    $('aiIdle').hidden = false;
    $('aiText').hidden = true;
    $('aiText').textContent = '';
    $('aiText').classList.remove('is-done');
    $('aiMeta').hidden = true;
    $('aiStartBtn').disabled = !state.meta;
    $('aiStartBtn').textContent = '生成总结';
  }

  async function startSummary() {
    if (!state.meta || state.summarizing) return;
    state.summarizing = true;
    const btn = $('aiStartBtn');
    btn.disabled = true;
    btn.textContent = '总结中…';

    $('aiIdle').hidden = true;
    $('aiText').hidden = false;
    $('aiText').textContent = '';
    $('aiText').classList.remove('is-done');
    $('aiMeta').hidden = true;

    try {
      const resp = await fetch('/api/summarize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: state.meta.url }),
      });
      if (!resp.ok || !resp.body) {
        let detail = '总结请求失败';
        try { detail = (await resp.json()).detail || detail; } catch (_) { /* ignore */ }
        throw new Error(detail);
      }
      await readSSE(resp, (data) => {
        if (data.type === 'meta') {
          const meta = $('aiMeta');
          meta.hidden = false;
          meta.classList.toggle('is-desc', data.source !== 'subtitle');
          meta.textContent = data.source === 'subtitle'
            ? `素材：字幕（${data.lang || 'unknown'}，${data.chars} 字）· ${data.model}`
            : `素材：简介${data.chars ? '（' + data.chars + ' 字）' : '（较少）'} · ${data.model}`;
        } else if (data.type === 'delta') {
          $('aiText').textContent += data.text;
        } else if (data.type === 'error') {
          throw new Error(data.message || '总结失败');
        }
      });
      $('aiText').classList.add('is-done');
      btn.textContent = '重新生成';
    } catch (err) {
      $('aiText').classList.add('is-done');
      if (!$('aiText').textContent) {
        $('aiText').textContent = '⚠ ' + err.message;
      } else {
        toast(err.message);
      }
      btn.textContent = '重新生成';
    } finally {
      state.summarizing = false;
      btn.disabled = false;
    }
  }

  async function readSSE(resp, onEvent) {
    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const lines = block.split('\n');
        for (const line of lines) {
          if (!line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          try { onEvent(JSON.parse(payload)); } catch (_) { /* ignore */ }
        }
      }
    }
  }

  /* ------------------------------------------------------------ 最近下载 */
  async function refreshJobs() {
    try {
      const data = await api('/api/jobs');
      const raw = JSON.stringify(data.jobs || []);
      if (raw === state.jobsRaw) return;
      state.jobsRaw = raw;
      state.jobs = data.jobs || [];
      if (state.page >= totalPages()) state.page = Math.max(0, totalPages() - 1);
      renderJobs();
    } catch (_) { /* ignore */ }
  }

  function totalPages() {
    return Math.max(1, Math.ceil(state.jobs.length / PAGE_SIZE));
  }

  function renderJobs() {
    const grid = $('recentGrid');
    grid.innerHTML = '';
    const list = state.jobs;

    if (!list.length) {
      $('gridEmpty').hidden = false;
      $('jobsPager').hidden = true;
      return;
    }
    $('gridEmpty').hidden = true;

    list.slice(state.page * PAGE_SIZE, state.page * PAGE_SIZE + PAGE_SIZE).forEach((job) => {
      const card = el('div', 'card');

      const cover = el('div', 'card__cover');
      if (job.thumbnail) {
        const img = el('img');
        img.src = job.thumbnail;
        img.alt = job.title || '';
        img.onerror = () => img.remove();
        cover.appendChild(img);
      }
      if (job.duration_text) cover.appendChild(el('span', 'card__dur', job.duration_text));
      if (job.status !== 'done') {
        const badge = el('span', 'card__badge' + (job.status === 'error' ? ' is-error' : ''),
          job.status === 'error' ? '失败' : `下载 ${Number(job.percent || 0).toFixed(0)}%`);
        cover.appendChild(badge);
      } else {
        cover.appendChild(el('span', 'card__badge is-done', '已完成'));
      }

      card.appendChild(cover);
      card.appendChild(el('div', 'card__title', job.title || '未命名视频'));
      const meta = el('div', 'card__meta');
      meta.appendChild(el('span', '', job.platform || '未知'));
      if (job.uploader) meta.appendChild(el('span', '', '· ' + job.uploader));
      card.appendChild(meta);

      const ops = el('div', 'card__ops');
      if (job.status === 'done') {
        const a = el('a', 'card__link', '重新下载');
        a.href = `/api/file/${job.id}`;
        ops.appendChild(a);
      }
      const del = el('button', 'card__del', '删除');
      del.type = 'button';
      del.addEventListener('click', async () => {
        try {
          await api(`/api/jobs/${job.id}`, { method: 'DELETE' });
          toast('已删除记录与文件');
          refreshJobs();
        } catch (err) {
          toast(err.message);
        }
      });
      ops.appendChild(del);
      card.appendChild(ops);
      grid.appendChild(card);
    });

    const pages = totalPages();
    $('jobsPager').hidden = pages <= 1;
    $('pagerInfo').textContent = `${state.page + 1} / ${pages}`;
    $('jobsPrev').disabled = state.page <= 0;
    $('jobsNext').disabled = state.page >= pages - 1;
  }

  /* ------------------------------------------------------------ 事件绑定 */
  function bind() {
    $('searchForm').addEventListener('submit', (e) => {
      e.preventDefault();
      doParse($('urlInput').value);
    });

    $('urlInput').addEventListener('input', (e) => {
      $('clearBtn').hidden = !e.target.value;
    });
    $('clearBtn').addEventListener('click', () => {
      $('urlInput').value = '';
      $('clearBtn').hidden = true;
      $('urlInput').focus();
    });

    $('examples').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-url]');
      if (!btn) return;
      $('urlInput').value = btn.dataset.url;
      $('clearBtn').hidden = false;
      doParse(btn.dataset.url);
    });

    $('downloadBtn').addEventListener('click', startDownload);

    $('summaryBtn').addEventListener('click', () => {
      if (!state.meta) return;
      $('aiPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
      startSummary();
    });
    $('aiStartBtn').addEventListener('click', startSummary);

    $('refreshJobs').addEventListener('click', () => {
      refreshJobs();
      toast('已刷新下载记录');
    });
    $('jobsPrev').addEventListener('click', () => {
      if (state.page > 0) { state.page -= 1; renderJobs(); }
    });
    $('jobsNext').addEventListener('click', () => {
      if (state.page < totalPages() - 1) { state.page += 1; renderJobs(); }
    });

    document.querySelectorAll('.nav__item').forEach((item) => {
      item.addEventListener('click', () => {
        document.querySelectorAll('.nav__item').forEach((n) => n.classList.toggle('is-active', n === item));
      });
    });

    // 页面可见时轻量轮询，让「最近下载」里的进度徽章保持更新
    setInterval(() => {
      if (!document.hidden) refreshJobs();
    }, 5000);
  }

  loadModels();
  refreshJobs();
  bind();
})();
