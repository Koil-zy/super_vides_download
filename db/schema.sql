-- 超级视频下载器 · 数据库建表语句（SQLite）
-- 时间统一用 UTC ISO8601 字符串（YYYY-MM-DDTHH:MM:SSZ），便于直接按字符串比较
-- 应用启动时由 app/db.py 执行本文件，全部语句均为 IF NOT EXISTS，可重复执行

-- 用户：邮箱 + 密码登录
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT    NOT NULL,
  nickname      TEXT    NOT NULL DEFAULT '',
  created_at    TEXT    NOT NULL
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
