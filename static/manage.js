(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    admin: null,
    view: '',
    rows: [],
    total: 0,
    page: 1,
    pages: 1,
    size: 15,
    q: '',
    authMode: 'login',
    role: 'merchant',
    codeTimer: 0,
    merchants: null,
    blacklistUser: null,
    configMode: 'create',
    configRow: null,
    confirmHandler: null,
  };

  /* ------------------------------------------------------------ 工具 */
  let toastTimer = 0;
  function toast(msg) {
    const el = $('toast');
    el.textContent = msg;
    el.classList.add('is-show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('is-show'), 2600);
  }

  async function api(path, options) {
    const resp = await fetch(path, options);
    if (!resp.ok) {
      let message = `请求失败（${resp.status}）`;
      let code = '';
      try {
        const data = await resp.json();
        const detail = data && data.detail;
        if (typeof detail === 'string') {
          message = detail;
        } else if (detail && typeof detail === 'object') {
          message = detail.message || message;
          code = detail.code || '';
        }
      } catch (_) { /* ignore */ }
      const err = new Error(message);
      err.status = resp.status;
      err.code = code;
      throw err;
    }
    return resp.json();
  }

  function textEl(cls, text) {
    const span = document.createElement('span');
    if (cls) span.className = cls;
    span.textContent = text == null ? '' : String(text);
    return span;
  }

  function badgeEl(text, cls) {
    const span = document.createElement('span');
    span.className = `badge ${cls || ''}`;
    span.textContent = text;
    return span;
  }

  function opBtn(label, cls, handler) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = cls || 'row-btn';
    btn.textContent = label;
    btn.addEventListener('click', handler);
    return btn;
  }

  function fmtTime(iso) {
    if (!iso) return '-';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString('zh-CN', { hour12: false }).replace(/\//g, '-');
  }

  function fmtDay(iso) {
    if (!iso) return '';
    return fmtTime(iso).split(' ')[0];
  }

  function showModal(id) {
    $(id).hidden = false;
  }

  function closeModal(id) {
    $(id).hidden = true;
  }

  function askConfirm(text, handler) {
    $('confirmText').textContent = text;
    state.confirmHandler = handler;
    showModal('confirmModal');
  }

  /* ------------------------------------------------------------ 登录 */
  function showAuth() {
    $('manageAuth').hidden = false;
    $('manageApp').hidden = true;
    $('adminAccount').hidden = true;
    $('loginBtn') && ($('loginBtn').hidden = true);
  }

  function setAuthMode(mode) {
    state.authMode = mode;
    document.querySelectorAll('.auth-tab').forEach((tab) => {
      tab.classList.toggle('is-active', tab.dataset.tab === mode);
    });
    $('roleField').hidden = mode !== 'register';
    $('codeField').hidden = mode !== 'register';
    $('nameField').hidden = mode !== 'register';
    $('codeHint').hidden = true;
    $('authError').hidden = true;
    $('authSubmit').textContent = mode === 'register' ? '注册并登录' : '登录';
    $('authPassword').autocomplete = mode === 'register' ? 'new-password' : 'current-password';
    setRole(state.role);
  }

  function setRole(role) {
    state.role = role === 'superadmin' ? 'superadmin' : 'merchant';
    document.querySelectorAll('.role-opt').forEach((btn) => {
      btn.classList.toggle('is-active', btn.dataset.role === state.role);
    });
    const isSuper = state.role === 'superadmin';
    $('initCodeField').hidden = state.authMode !== 'register' || !isSuper;
    $('nameLabel').textContent = isSuper ? '姓名（可选）' : '商户名称（可选）';
  }

  async function sendCode() {
    const phone = $('authPhone').value.trim();
    if (!/^1[3-9]\d{9}$/.test(phone)) {
      showAuthError('请输入正确的 11 位手机号');
      return;
    }
    const btn = $('sendCodeBtn');
    btn.disabled = true;
    try {
      const data = await api('/api/admin/sms/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone, scene: 'register' }),
      });
      const hint = $('codeHint');
      if (data.mock && data.dev_code) {
        $('authCode').value = data.dev_code;
        hint.textContent = `测试环境短信：验证码 ${data.dev_code}（5 分钟内有效，已自动填入）`;
        hint.hidden = false;
      } else {
        hint.textContent = '验证码已通过中国移动短信通道发送，请注意查收';
        hint.hidden = false;
      }
      startCountdown(data.interval || 60);
    } catch (err) {
      showAuthError(err.message);
      btn.disabled = false;
    }
  }

  function startCountdown(seconds) {
    clearInterval(state.codeTimer);
    let left = seconds;
    const btn = $('sendCodeBtn');
    btn.disabled = true;
    btn.textContent = `${left}s 后重发`;
    state.codeTimer = setInterval(() => {
      left -= 1;
      if (left <= 0) {
        clearInterval(state.codeTimer);
        btn.disabled = false;
        btn.textContent = '重新获取';
        return;
      }
      btn.textContent = `${left}s 后重发`;
    }, 1000);
  }

  function showAuthError(text) {
    const el = $('authError');
    el.textContent = text;
    el.hidden = false;
  }

  async function submitAuth(e) {
    e.preventDefault();
    const phone = $('authPhone').value.trim();
    const password = $('authPassword').value;
    const btn = $('authSubmit');
    $('authError').hidden = true;
    btn.disabled = true;
    btn.textContent = state.authMode === 'register' ? '注册中…' : '登录中…';
    try {
      const body = state.authMode === 'register'
        ? {
          phone,
          password,
          code: $('authCode').value.trim(),
          name: $('authName').value.trim(),
          role: state.role,
          init_code: state.role === 'superadmin' ? $('authInitCode').value.trim() : '',
        }
        : { phone, password };
      const data = await api(state.authMode === 'register' ? '/api/admin/register' : '/api/admin/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      $('authForm').reset();
      $('codeHint').hidden = true;
      enterApp(data.admin);
      toast(state.authMode === 'register' ? '注册成功，已进入管理端' : '登录成功');
    } catch (err) {
      showAuthError(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = state.authMode === 'register' ? '注册并登录' : '登录';
    }
  }

  async function logout() {
    try {
      await api('/api/admin/logout', { method: 'POST' });
    } catch (_) { /* ignore */ }
    state.admin = null;
    state.rows = [];
    state.view = '';
    showAuth();
    setAuthMode('login');
    toast('已退出管理端');
  }

  /* ------------------------------------------------------------ 进入后台 */
  const VIEWS = {
    users: {
      title: '用户管理',
      sub: '新增用户、调整会员状态，或将用户拉入黑名单（逻辑删除）',
      placeholder: '按邮箱或昵称搜索',
      action: '新增用户',
      roles: ['superadmin'],
    },
    blacklist: {
      title: '黑名单',
      sub: '被逻辑删除的用户；可在这里恢复其登录权限',
      placeholder: '按邮箱或昵称搜索',
      action: '',
      roles: ['superadmin'],
    },
    downloads: {
      title: '下载记录',
      sub: '全部用户的下载记录（只读）：视频名称、平台来源与下载时间',
      placeholder: '按用户账号搜索',
      action: '',
      roles: ['superadmin'],
    },
    myusers: {
      title: '我的用户',
      sub: '通过本商户邀请码注册的用户（只读）',
      placeholder: '按邮箱或昵称搜索',
      action: '',
      roles: ['merchant'],
    },
    pay: {
      title: '支付配置',
      sub: '微信 / 支付宝密钥配置，修改会生成新版本，历史记录不可修改',
      placeholder: '',
      action: '新增配置',
      roles: ['merchant'],
    },
  };

  function enterApp(admin) {
    state.admin = admin;
    $('manageAuth').hidden = true;
    $('manageApp').hidden = false;
    $('adminAccount').hidden = false;
    const name = admin.name || admin.phone;
    $('adminAvatar').textContent = name.slice(0, 1).toUpperCase();
    $('adminName').textContent = name;
    $('adminRole').textContent = admin.role_label;
    $('sideAvatar').textContent = name.slice(0, 1).toUpperCase();
    $('sideName').textContent = name;
    $('sideRole').textContent = admin.role_label;
    $('sidePhone').textContent = admin.phone;
    const isMerchant = admin.role === 'merchant';
    $('sideInviteRow').hidden = !isMerchant;
    $('sideInviteTip').hidden = !isMerchant;
    if (isMerchant) $('sideInviteCode').textContent = admin.merchant_code;

    document.querySelectorAll('.side-nav__item').forEach((item) => {
      const allowed = VIEWS[item.dataset.view].roles.includes(admin.role);
      item.hidden = !allowed;
    });
    switchView(isMerchant ? 'myusers' : 'users');
  }

  function switchView(view) {
    state.view = view;
    state.page = 1;
    state.q = '';
    $('searchInput').value = '';
    document.querySelectorAll('.side-nav__item').forEach((item) => {
      item.classList.toggle('is-active', item.dataset.view === view);
    });
    loadView();
  }

  async function loadView() {
    const def = VIEWS[state.view];
    if (!def) return;
    $('viewTitle').textContent = def.title;
    $('viewSub').textContent = def.sub;
    $('searchInput').placeholder = def.placeholder || '搜索';
    const searchWrap = $('searchInput').closest('.manage-search');
    searchWrap.hidden = !def.placeholder;
    $('searchBtn').hidden = !def.placeholder;
    $('primaryAction').hidden = !def.action;
    $('primaryAction').textContent = def.action || '';
    if (state.view === 'pay') {
      await loadPayConfigs();
      return;
    }
    const paths = {
      users: '/api/admin/users',
      myusers: '/api/admin/users',
      blacklist: '/api/admin/blacklist',
      downloads: '/api/admin/downloads',
    };
    const params = new URLSearchParams({ page: state.page, size: state.size });
    if (state.q) params.set('q', state.q);
    try {
      const data = await api(`${paths[state.view]}?${params.toString()}`);
      state.rows = data.items || [];
      state.total = data.total || 0;
      state.pages = data.pages || 1;
      state.page = data.page || 1;
    } catch (err) {
      handleAuthError(err);
      state.rows = [];
      state.total = 0;
      state.pages = 1;
    }
    renderTable();
  }

  async function loadPayConfigs() {
    try {
      const data = await api('/api/admin/pay/configs');
      state.rows = data.items || [];
      state.total = state.rows.length;
      state.pages = 1;
      state.page = 1;
    } catch (err) {
      handleAuthError(err);
      state.rows = [];
      state.total = 0;
      state.pages = 1;
    }
    renderTable();
  }

  function handleAuthError(err) {
    if (err.status === 401) {
      showAuth();
      setAuthMode('login');
      toast('登录已过期，请重新登录');
      return;
    }
    toast(err.message);
  }

  /* ------------------------------------------------------------ 表格 */
  const COLUMNS = {
    users: [
      { label: '邮箱', render: (r) => textEl('cell-strong', r.email) },
      { label: '昵称', render: (r) => r.nickname || '-' },
      { label: '归属商户', render: (r) => r.merchant_name || '未归属' },
      { label: '会员', render: (r) => (r.premium ? badgeEl(`会员至 ${fmtDay(r.premium_until)}`, 'badge--vip') : badgeEl('免费', 'badge--free')) },
      { label: '注册时间', render: (r) => textEl('cell-time', fmtTime(r.created_at)) },
      { label: '操作', render: (r) => userOps(r) },
    ],
    myusers: [
      { label: '邮箱', render: (r) => textEl('cell-strong', r.email) },
      { label: '昵称', render: (r) => r.nickname || '-' },
      { label: '会员', render: (r) => (r.premium ? badgeEl(`会员至 ${fmtDay(r.premium_until)}`, 'badge--vip') : badgeEl('免费', 'badge--free')) },
      { label: '状态', render: (r) => (r.blacklisted ? badgeEl('已拉黑', 'badge--banned') : badgeEl('正常', 'badge--active')) },
      { label: '注册时间', render: (r) => textEl('cell-time', fmtTime(r.created_at)) },
    ],
    blacklist: [
      { label: '邮箱', render: (r) => textEl('cell-strong', r.email) },
      { label: '昵称', render: (r) => r.nickname || '-' },
      { label: '拉黑时间', render: (r) => textEl('cell-time', fmtTime(r.blacklisted_at)) },
      { label: '原因', render: (r) => r.blacklist_reason || '-' },
      { label: '操作', render: (r) => restoreOps(r) },
    ],
    downloads: [
      {
        label: '用户账号',
        render: (r) => {
          const box = document.createElement('div');
          box.appendChild(textEl('cell-strong', r.email));
          if (r.nickname) {
            const small = document.createElement('small');
            small.textContent = r.nickname;
            box.appendChild(small);
          }
          return box;
        },
      },
      {
        label: '视频名称',
        render: (r) => {
          const span = textEl('cell-ellipsis', r.title || '未命名视频');
          span.title = r.title || '';
          return span;
        },
      },
      { label: '平台来源', render: (r) => badgeEl(r.platform || '未知', 'badge--history') },
      { label: '清晰度', render: (r) => r.quality || '-' },
      { label: '下载时间', render: (r) => textEl('cell-time', fmtTime(r.created_at)) },
    ],
    pay: [
      { label: '渠道', render: (r) => badgeEl(r.provider_label, r.provider === 'wechat' ? 'badge--vip' : 'badge--active') },
      { label: 'AppID', render: (r) => r.app_id || '-' },
      { label: '商户号', render: (r) => r.mchid || '-' },
      { label: '状态', render: (r) => badgeEl(r.status_label, r.status === 'active' ? 'badge--active' : 'badge--history') },
      { label: '创建时间', render: (r) => textEl('cell-time', fmtTime(r.created_at)) },
      { label: '操作', render: (r) => payOps(r) },
    ],
  };

  function userOps(row) {
    const box = document.createElement('div');
    box.className = 'row-ops';
    box.appendChild(opBtn(row.premium ? '取消会员' : '开通会员', row.premium ? 'row-btn row-btn--pink' : 'row-btn',
      () => togglePremium(row)));
    box.appendChild(opBtn('拉黑', 'row-btn row-btn--danger', () => openBlacklist(row)));
    return box;
  }

  function restoreOps(row) {
    const box = document.createElement('div');
    box.className = 'row-ops';
    box.appendChild(opBtn('恢复用户', 'row-btn', () => restoreUser(row)));
    return box;
  }

  function payOps(row) {
    const box = document.createElement('div');
    box.className = 'row-ops';
    box.appendChild(opBtn('查看', 'row-btn', () => openConfigModal('view', row)));
    if (row.editable) {
      box.appendChild(opBtn('修改', 'row-btn row-btn--pink', () => openConfigModal('edit', row)));
    }
    if (row.deletable) {
      box.appendChild(opBtn('删除', 'row-btn row-btn--danger', () => deleteConfig(row)));
    }
    return box;
  }

  function renderTable() {
    const cols = COLUMNS[state.view] || [];
    const head = $('tableHead');
    const body = $('tableBody');
    head.innerHTML = '';
    body.innerHTML = '';
    const headRow = document.createElement('tr');
    cols.forEach((col) => {
      const th = document.createElement('th');
      th.textContent = col.label;
      headRow.appendChild(th);
    });
    head.appendChild(headRow);

    const rows = state.rows || [];
    $('tableEmpty').hidden = rows.length > 0;
    rows.forEach((row) => {
      const tr = document.createElement('tr');
      cols.forEach((col) => {
        const td = document.createElement('td');
        const content = col.render(row);
        if (content instanceof Node) td.appendChild(content);
        else td.textContent = content == null ? '-' : String(content);
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });

    $('pager').hidden = state.pages <= 1;
    $('pageInfo').textContent = `第 ${state.page} / ${state.pages} 页 · 共 ${state.total} 条`;
    $('prevPage').disabled = state.page <= 1;
    $('nextPage').disabled = state.page >= state.pages;
  }

  /* ------------------------------------------------------------ 用户操作 */
  async function togglePremium(row) {
    try {
      await api(`/api/admin/users/${row.id}/premium`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ premium: !row.premium }),
      });
      toast(row.premium ? '已取消会员' : '已开通 30 天会员');
      loadView();
    } catch (err) {
      handleAuthError(err);
    }
  }

  function openBlacklist(row) {
    state.blacklistUser = row;
    $('blacklistSub').textContent = `拉黑后 ${row.email} 将无法登录与下载，可在黑名单页恢复`;
    $('blacklistReason').value = '';
    $('blacklistError').hidden = true;
    showModal('blacklistModal');
  }

  async function confirmBlacklist() {
    const row = state.blacklistUser;
    if (!row) return;
    const btn = $('blacklistSubmit');
    btn.disabled = true;
    try {
      await api(`/api/admin/users/${row.id}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: $('blacklistReason').value.trim() }),
      });
      closeModal('blacklistModal');
      toast('已加入黑名单');
      loadView();
    } catch (err) {
      $('blacklistError').textContent = err.message;
      $('blacklistError').hidden = false;
    } finally {
      btn.disabled = false;
    }
  }

  function restoreUser(row) {
    askConfirm(`确定将 ${row.email} 移出黑名单并恢复登录权限？`, async () => {
      try {
        await api(`/api/admin/users/${row.id}/restore`, { method: 'POST' });
        toast('已恢复该用户');
        loadView();
      } catch (err) {
        handleAuthError(err);
      }
    });
  }

  async function openUserModal() {
    $('userForm').reset();
    $('userError').hidden = true;
    try {
      if (!state.merchants) {
        const data = await api('/api/admin/merchants');
        state.merchants = data.items || [];
      }
      const select = $('userMerchant');
      select.innerHTML = '';
      const none = document.createElement('option');
      none.value = '';
      none.textContent = '不归属商户';
      select.appendChild(none);
      state.merchants.forEach((m) => {
        const opt = document.createElement('option');
        opt.value = String(m.id);
        opt.textContent = `${m.name || m.phone}（${m.merchant_code}）`;
        select.appendChild(opt);
      });
    } catch (err) {
      handleAuthError(err);
      return;
    }
    showModal('userModal');
  }

  async function submitUser(e) {
    e.preventDefault();
    const btn = $('userSubmit');
    const error = $('userError');
    error.hidden = true;
    btn.disabled = true;
    try {
      await api('/api/admin/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: $('userEmail').value.trim(),
          password: $('userPassword').value,
          nickname: $('userNickname').value.trim(),
          merchant_id: $('userMerchant').value ? Number($('userMerchant').value) : null,
          premium: $('userPremium').checked,
        }),
      });
      closeModal('userModal');
      toast('用户创建成功');
      loadView();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    } finally {
      btn.disabled = false;
    }
  }

  /* ------------------------------------------------------------ 支付配置 */
  function toggleProviderFields() {
    const wechat = $('configProvider').value === 'wechat';
    $('wxFields').hidden = !wechat;
    $('aliFields').hidden = wechat;
  }

  async function openConfigModal(mode, row) {
    state.configMode = mode;
    state.configRow = row || null;
    $('configForm').reset();
    $('configError').hidden = true;
    $('configProvider').disabled = mode !== 'create';
    $('configSubmit').hidden = mode === 'view';
    $('configSubmit').textContent = mode === 'edit' ? '保存为新版本' : '保存配置';

    let detail = null;
    if (row) {
      $('configSubmit').disabled = true;
      try {
        const data = await api(`/api/admin/pay/configs/${row.id}`);
        detail = data.config;
      } catch (err) {
        handleAuthError(err);
        return;
      }
      $('configSubmit').disabled = false;
    }

    const provider = detail ? detail.provider : (row && row.provider) || 'wechat';
    $('configProvider').value = provider;
    $('configTitle').textContent = mode === 'create' ? '新增支付配置' : (mode === 'edit' ? '修改支付配置' : '查看支付配置');
    $('configSub').textContent = mode === 'create'
      ? '保存后会立即成为当前生效配置，原有生效配置转为历史版本'
      : (mode === 'edit' ? '保存后生成新版本，旧配置保留为历史记录且不可修改' : '历史配置只读；如需调整请新增当前生效配置');

    const fields = ['cfgAppId', 'cfgMchid', 'cfgApiKey', 'cfgAliAppId', 'cfgPrivateKey', 'cfgPublicKey'];
    fields.forEach((id) => { $(id).value = ''; $(id).disabled = mode === 'view'; });
    if (detail) {
      if (detail.provider === 'wechat') {
        $('cfgAppId').value = detail.app_id || '';
        $('cfgMchid').value = detail.mchid || '';
        $('cfgApiKey').value = detail.api_key || '';
      } else {
        $('cfgAliAppId').value = detail.app_id || '';
        $('cfgPrivateKey').value = detail.private_key || '';
        $('cfgPublicKey').value = detail.public_key || '';
      }
    }
    toggleProviderFields();
    showModal('configModal');
  }

  async function submitConfig(e) {
    e.preventDefault();
    const mode = state.configMode;
    const provider = $('configProvider').value;
    const body = provider === 'wechat'
      ? {
        provider,
        app_id: $('cfgAppId').value.trim(),
        mchid: $('cfgMchid').value.trim(),
        api_key: $('cfgApiKey').value.trim(),
      }
      : {
        provider,
        app_id: $('cfgAliAppId').value.trim(),
        private_key: $('cfgPrivateKey').value.trim(),
        public_key: $('cfgPublicKey').value.trim(),
      };
    const submit = $('configSubmit');
    const error = $('configError');
    error.hidden = true;
    submit.disabled = true;
    try {
      const path = mode === 'edit' ? `/api/admin/pay/configs/${state.configRow.id}` : '/api/admin/pay/configs';
      await api(path, {
        method: mode === 'edit' ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      closeModal('configModal');
      toast(mode === 'edit' ? '已生成新的配置版本' : '配置已保存');
      loadView();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    } finally {
      submit.disabled = false;
    }
  }

  function deleteConfig(row) {
    askConfirm(`确定删除 ${row.provider_label} 配置（${row.app_id}）？删除后不再出现在列表中。`, async () => {
      try {
        await api(`/api/admin/pay/configs/${row.id}`, { method: 'DELETE' });
        toast('配置已删除');
        loadView();
      } catch (err) {
        handleAuthError(err);
      }
    });
  }

  /* ------------------------------------------------------------ 事件绑定 */
  function bind() {
    document.querySelectorAll('.auth-tab').forEach((tab) => {
      tab.addEventListener('click', () => setAuthMode(tab.dataset.tab));
    });
    document.querySelectorAll('.role-opt').forEach((btn) => {
      btn.addEventListener('click', () => setRole(btn.dataset.role));
    });
    $('authForm').addEventListener('submit', submitAuth);
    $('sendCodeBtn').addEventListener('click', sendCode);

    document.querySelectorAll('[data-close]').forEach((node) => {
      node.addEventListener('click', () => closeModal(node.dataset.close));
    });

    document.querySelectorAll('.side-nav__item').forEach((item) => {
      item.addEventListener('click', () => switchView(item.dataset.view));
    });

    $('adminLogout').addEventListener('click', logout);
    $('searchBtn').addEventListener('click', () => {
      state.q = $('searchInput').value.trim();
      state.page = 1;
      loadView();
    });
    $('searchInput').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        state.q = $('searchInput').value.trim();
        state.page = 1;
        loadView();
      }
    });
    $('prevPage').addEventListener('click', () => {
      if (state.page > 1) { state.page -= 1; loadView(); }
    });
    $('nextPage').addEventListener('click', () => {
      if (state.page < state.pages) { state.page += 1; loadView(); }
    });
    $('primaryAction').addEventListener('click', () => {
      if (state.view === 'users') openUserModal();
      else if (state.view === 'pay') openConfigModal('create');
    });

    $('userForm').addEventListener('submit', submitUser);
    $('blacklistSubmit').addEventListener('click', confirmBlacklist);
    $('configForm').addEventListener('submit', submitConfig);
    $('configProvider').addEventListener('change', toggleProviderFields);
    $('confirmCancel').addEventListener('click', () => closeModal('confirmModal'));
    $('confirmOk').addEventListener('click', () => {
      const handler = state.confirmHandler;
      state.confirmHandler = null;
      closeModal('confirmModal');
      if (handler) handler();
    });
  }

  async function boot() {
    bind();
    showAuth();
    setAuthMode('login');
    try {
      const data = await api('/api/admin/me');
      enterApp(data.admin);
    } catch (_) {
      showAuth();
    }
  }

  boot();
})();
