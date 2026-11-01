-- 超级视频下载器 · 数据库建表语句（SQLite）
-- 时间统一用 UTC ISO8601 字符串（YYYY-MM-DDTHH:MM:SSZ），便于直接按字符串比较
-- 应用启动时由 app/db.py 执行本文件，全部语句均为 IF NOT EXISTS，可重复执行

-- 用户：邮箱 + 密码登录
-- merchant_id     归属商户（admins.id），为空表示无归属；由注册时填写的邀请码决定
-- blacklisted_at  非空即已被管理端拉黑（逻辑删除），不物理删除用户
CREATE TABLE IF NOT EXISTS users (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  email          TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  password_hash  TEXT    NOT NULL,
  nickname       TEXT    NOT NULL DEFAULT '',
  created_at     TEXT    NOT NULL,
  merchant_id    INTEGER REFERENCES admins(id),
  blacklisted_at TEXT,
  blacklist_reason TEXT NOT NULL DEFAULT ''
);

-- 登录会话：token 存在 HttpOnly Cookie 里，服务端只存哈希前的随机串
CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT    PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT    NOT NULL,
  expires_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- 支付订单：一个订单对应一次二维码支付
CREATE TABLE IF NOT EXISTS orders (
  id           TEXT    PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider     TEXT    NOT NULL CHECK (provider IN ('wechat', 'alipay')),
  plan         TEXT    NOT NULL,
  amount_cents INTEGER NOT NULL,
  status       TEXT    NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'paid', 'expired')),
  qr_content   TEXT    NOT NULL,
  created_at   TEXT    NOT NULL,
  paid_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at);

-- 会员订阅：支付成功后写入，expires_at 到期即失效
CREATE TABLE IF NOT EXISTS subscriptions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  plan       TEXT    NOT NULL,
  started_at TEXT    NOT NULL,
  expires_at TEXT    NOT NULL,
  order_id   TEXT    REFERENCES orders(id)
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id, expires_at);

-- ==================================================================
-- 以下是管理端相关表
-- ==================================================================

-- 管理端账号：手机号 + 密码 + 短信验证码注册；role 决定权限
-- merchant_code 为商户邀请码，普通用户注册时填写后归属该商户
CREATE TABLE IF NOT EXISTS admins (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  phone         TEXT    NOT NULL UNIQUE,
  password_hash TEXT    NOT NULL,
  name          TEXT    NOT NULL DEFAULT '',
  role          TEXT    NOT NULL DEFAULT 'merchant'
                        CHECK (role IN ('merchant', 'superadmin')),
  merchant_code TEXT    NOT NULL UNIQUE,
  status        TEXT    NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'disabled')),
  created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admins_role ON admins(role, status);

-- 支付渠道配置（按商户维度）：修改配置不覆盖旧记录，而是新增一条新版本
-- status: active 当前生效 / superseded 已被新版本取代 / deleted 已删除
CREATE TABLE IF NOT EXISTS payment_configs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  admin_id    INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
  provider    TEXT    NOT NULL CHECK (provider IN ('wechat', 'alipay')),
  app_id      TEXT    NOT NULL DEFAULT '',
  mchid       TEXT    NOT NULL DEFAULT '',
  api_key     TEXT    NOT NULL DEFAULT '',
  private_key TEXT    NOT NULL DEFAULT '',
  public_key  TEXT    NOT NULL DEFAULT '',
  status      TEXT    NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'superseded', 'deleted')),
  created_at  TEXT    NOT NULL,
  deleted_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pay_configs_admin ON payment_configs(admin_id, provider, status);

-- 用户下载记录：只在管理端查看，记录视频名称 / 时间 / 平台来源，不涉及文件
CREATE TABLE IF NOT EXISTS download_records (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  merchant_id INTEGER REFERENCES admins(id),
  job_id      TEXT    NOT NULL DEFAULT '',
  title       TEXT    NOT NULL DEFAULT '',
  platform    TEXT    NOT NULL DEFAULT '',
  format      TEXT    NOT NULL DEFAULT '',
  height      INTEGER NOT NULL DEFAULT 0,
  url         TEXT    NOT NULL DEFAULT '',
  created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_download_records_user ON download_records(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_download_records_time ON download_records(created_at);

-- ==================================================================
-- 存量数据库迁移说明（SQLite 不支持 ADD COLUMN IF NOT EXISTS）
-- app/db.py 会在启动时检查 users 表缺失的列并自动补齐：
--   ALTER TABLE users ADD COLUMN merchant_id INTEGER REFERENCES admins(id);
--   ALTER TABLE users ADD COLUMN blacklisted_at TEXT;
--   ALTER TABLE users ADD COLUMN blacklist_reason TEXT NOT NULL DEFAULT '';
-- ==================================================================
