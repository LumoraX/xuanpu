-- 桌面客户端的浏览器接力登录（ADR 0021）。
-- 登录尝试记下发起的客户端：browser 照旧在浏览器里完成；desktop 的回调落在系统浏览器，
-- 回调不核对登录尝试 cookie（它留在桌面客户端里），改为建一条桌面接力记录。
-- 全部写成可重复执行：这组表结构曾以 0026_desktop_handoff 部署到生产，与主线的
-- 0026_local_credential 撞号，改号为 0027 后已有这些对象的库重跑也不出错（见 ADR 0021）。
ALTER TABLE "${schema}"."${prefix}login_attempt"
    ADD COLUMN IF NOT EXISTS client TEXT NOT NULL DEFAULT 'browser';

-- 桌面接力：飞书已回调并认出员工，等员工在浏览器里确认、桌面客户端来换取登录会话。
-- cookie_hash 是桌面客户端登录尝试 cookie 的摘要（换取凭它）；nonce_hash 是浏览器确认页里
-- 一次性确认串的摘要（确认凭它）。两者都只存摘要。没走完的由维护任务按 expires_at 清理。
CREATE TABLE IF NOT EXISTS "${schema}"."${prefix}desktop_handoff" (
    cookie_hash TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant" (tenant_id),
    user_id     TEXT COLLATE "C" NOT NULL,
    return_to   TEXT NOT NULL,
    nonce_hash  TEXT NOT NULL UNIQUE,
    confirmed   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  DOUBLE PRECISION NOT NULL,
    expires_at  DOUBLE PRECISION NOT NULL,
    FOREIGN KEY (tenant_id, user_id) REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id)
);
CREATE INDEX IF NOT EXISTS "${prefix}desktop_handoff_expiry_idx"
    ON "${schema}"."${prefix}desktop_handoff" (expires_at);
