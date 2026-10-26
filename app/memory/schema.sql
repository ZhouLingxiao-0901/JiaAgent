-- ============================================================
-- JiaAgent 数据库结构（SQLite）
--
-- 阶段一真正用到的只有 sessions 和 messages 两张表。
-- memories 表先建好占位：阶段二做"长期记忆"时直接用，不用改表结构。
-- ============================================================

-- 会话：一次对话的容器
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,          -- 随机生成的 32 位字符串
    title       TEXT,                      -- 标题，自动取第一条用户消息的前 20 字
    model       TEXT,                      -- 这个会话用的哪个模型
    created_at  TEXT NOT NULL,             -- 创建时间（ISO8601 文本）
    updated_at  TEXT NOT NULL              -- 最后活动时间
);

-- 消息：这就是"短期记忆"，模型每次能看到的上下文
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content     TEXT NOT NULL,
    model       TEXT,                      -- 生成这条回复用的模型
    tokens_in   INTEGER,                   -- 阶段二再统计，先留空
    tokens_out  INTEGER,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, id);

-- 长期记忆：跨会话记住的事实、偏好、设定
-- 阶段一只建表，不自动写入；阶段二接"自动提取 + 检索"
CREATE TABLE IF NOT EXISTS memories (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL,       -- fact（事实）/ preference（偏好）/ task（任务）
    key               TEXT,                -- 可选的去重键，比如"用户称呼"
    content           TEXT NOT NULL,
    importance        INTEGER NOT NULL DEFAULT 3,   -- 1 到 5，越大越重要
    source_session_id TEXT,                -- 这条记忆是从哪次对话里来的
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories (kind, importance DESC);
