"""SQLite 연결과 테이블 정의."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 시험할 때는 BRIEFING_DB로 복사본 DB를 지정해 실제 기록을 건드리지 않음
DB_PATH = Path(os.getenv("BRIEFING_DB") or ROOT / "data" / "briefing.db")

# 1인 단계에서는 사용자가 나 한 명 (공유 단계에서 로그인 추가)
DEFAULT_USER_ID = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY,
    link          TEXT NOT NULL UNIQUE,
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    rss_summary   TEXT,
    published_at  TEXT NOT NULL,          -- 한국 시간 ISO 형식
    has_time      INTEGER NOT NULL DEFAULT 1,  -- 0이면 화면에 시각을 숨김
    fetched_at    TEXT NOT NULL,
    ai_summary    TEXT,                   -- 4단계: 추천된 기사만 AI 요약
    summary_status TEXT NOT NULL DEFAULT 'none',  -- none / pending / done / failed / unavailable
    summary_source TEXT,                  -- body: 원문 본문 / rss: RSS 요약문으로 요약
    summary_attempts INTEGER NOT NULL DEFAULT 0,
    summary_updated_at TEXT,
    embedding     BLOB                    -- 3단계에서 채움
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);

-- 기사가 어느 분야 피드에 실렸는지 (한 기사가 여러 분야에 있을 수 있음)
CREATE TABLE IF NOT EXISTS article_categories (
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    category   TEXT NOT NULL,
    PRIMARY KEY (article_id, category)
);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    onboarded  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS interests (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text      TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('like', 'dislike')),
    embedding BLOB,                       -- 3단계에서 채움
    UNIQUE (user_id, text, kind)
);

-- 행동 기록 원본 (신경망 학습용으로 모두 남김)
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    type       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, created_at);

-- 기사별 현재 상태 (화면에서 빠르게 쓰기 위한 요약본)
CREATE TABLE IF NOT EXISTS article_state (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    liked      INTEGER NOT NULL DEFAULT 0,
    disliked   INTEGER NOT NULL DEFAULT 0,
    hidden     INTEGER NOT NULL DEFAULT 0,
    read       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, article_id)
);

CREATE TABLE IF NOT EXISTS feed_status (
    url             TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    category        TEXT NOT NULL,
    last_success_at TEXT,
    last_error      TEXT,
    last_count      INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # 수집 중에도 화면 요청을 읽을 수 있게
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# 이미 만들어진 DB에 나중에 추가한 칸 (테이블, 칸, 정의)
MIGRATIONS = [
    ("articles", "summary_source", "TEXT"),
    ("articles", "summary_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("articles", "summary_updated_at", "TEXT"),
]


def init() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.execute("INSERT OR IGNORE INTO users (id, name) VALUES (?, ?)", (DEFAULT_USER_ID, "나"))


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
