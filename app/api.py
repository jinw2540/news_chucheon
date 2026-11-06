"""API 경로 (PLAN.md 7-4)."""
import sqlite3
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import db, embedder, recommender, summarizer, weather
from .collector import KST, load_feeds

router = APIRouter(prefix="/api")
USER = db.DEFAULT_USER_ID
PER_SOURCE_IN_TAB = 5

SUGGESTED_INTERESTS = ["반도체", "부동산", "AI", "야구", "환율", "기후", "게임", "교육", "전기차", "우주", "건강", "여행"]

ARTICLE_SQL = """
SELECT a.id, a.source, a.title, a.link, a.rss_summary, a.published_at, a.has_time,
       a.ai_summary, a.summary_status,
       COALESCE(GROUP_CONCAT(DISTINCT c.category), '') AS cats,
       COALESCE(s.liked, 0) AS liked, COALESCE(s.read, 0) AS read
FROM articles a
LEFT JOIN article_categories c ON c.article_id = a.id
LEFT JOIN article_state s ON s.article_id = a.id AND s.user_id = :user
WHERE COALESCE(s.hidden, 0) = 0 AND COALESCE(s.disliked, 0) = 0
"""


def _rows(conn: sqlite3.Connection, extra: str = "", with_vec: bool = False, **params) -> list[dict]:
    sql = ARTICLE_SQL + extra + " GROUP BY a.id ORDER BY a.published_at DESC"
    if with_vec:
        sql = sql.replace("a.summary_status,", "a.summary_status, a.embedding,", 1)
    out = []
    for r in conn.execute(sql, {"user": USER, **params}):
        d = dict(r)
        d["categories"] = [c for c in d.pop("cats").split(",") if c]
        if with_vec:
            blob = d.pop("embedding")
            d["vec"] = embedder.from_blob(blob) if blob else None
        out.append(d)
    return out


def _public(a: dict) -> dict:
    """화면에 보낼 모양. 요약은 AI 요약이 있으면 그것, 없으면 RSS 요약문."""
    if a.get("ai_summary"):
        summary, kind = a["ai_summary"], "ai"
    elif a.get("rss_summary"):
        summary, kind = a["rss_summary"], "rss"
    else:
        summary, kind = None, None
    return {
        "id": a["id"], "source": a["source"], "title": a["title"], "link": a["link"],
        "published_at": a["published_at"], "has_time": bool(a["has_time"]),
        "categories": a["categories"], "summary": summary, "summary_kind": kind,
        "summary_status": a["summary_status"], "liked": bool(a["liked"]), "read": bool(a["read"]),
    }


def _interests(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    rows = conn.execute("SELECT text, kind FROM interests WHERE user_id = ? ORDER BY id", (USER,)).fetchall()
    return [r["text"] for r in rows if r["kind"] == "like"], [r["text"] for r in rows if r["kind"] == "dislike"]


def _interest_vectors(conn: sqlite3.Connection) -> tuple[list[tuple], list[tuple]]:
    """[(글, 벡터|None)] 관심 있는 것 / 관심 없는 것"""
    rows = conn.execute("SELECT text, kind, embedding FROM interests WHERE user_id = ? ORDER BY id", (USER,)).fetchall()
    pair = lambda r: (r["text"], embedder.from_blob(r["embedding"]) if r["embedding"] else None)
    return [pair(r) for r in rows if r["kind"] == "like"], [pair(r) for r in rows if r["kind"] == "dislike"]


def _state_vectors(conn: sqlite3.Connection, column: str, limit: int = 50) -> list[tuple[str, "np.ndarray"]]:
    """좋아요/별로 처리한 기사의 (제목, 벡터) (최근 행동 순)"""
    rows = conn.execute(
        f"""SELECT a.title, a.embedding FROM article_state s JOIN articles a ON a.id = s.article_id
            WHERE s.user_id = ? AND s.{column} = 1 AND a.embedding IS NOT NULL
            ORDER BY (SELECT MAX(e.id) FROM events e WHERE e.user_id = s.user_id AND e.article_id = s.article_id) DESC
            LIMIT ?""", (USER, limit)).fetchall()
    return [(r["title"], embedder.from_blob(r["embedding"])) for r in rows]


def _context() -> "recommender.Context":
    """추천과 설명에 쓰는 오늘의 점수 계산."""
    with db.session() as conn:
        likes, dislikes = _interest_vectors(conn)
        liked = _state_vectors(conn, "liked")
        disliked_vecs = [v for _, v in _state_vectors(conn, "disliked")]
        since = datetime.now(KST) - timedelta(hours=recommender.RECENT_HOURS)
        articles = _rows(conn, " AND a.published_at >= :since", with_vec=True, since=since.isoformat(timespec="seconds"))
    # 아직 벡터가 없는 새 기사는 빼고 추천 (다음 수집 때 벡터가 생김)
    with_vec = [a for a in articles if a["vec"] is not None]
    if len(with_vec) >= len(articles) * 0.5:
        articles = with_vec
    return recommender.build_context(articles, likes, dislikes, liked, disliked_vecs)


# ── 기본 정보 ───────────────────────────────────────────

@router.get("/meta")
def meta():
    cfg = load_feeds()
    sources = list(dict.fromkeys(f["source"] for f in cfg["feeds"]))
    with db.session() as conn:
        failed = conn.execute("SELECT COUNT(*) FROM feed_status WHERE last_error IS NOT NULL").fetchone()[0]
        return {
            "sources": sources,
            "categories": ["전체", *cfg["categories"]],
            "updated_at": db.get_meta(conn, "last_collect_at"),
            "feeds_failed": failed,
            "suggested_interests": SUGGESTED_INTERESTS,
        }


@router.get("/health")
def health():
    with db.session() as conn:
        return {
            "last_collect_at": db.get_meta(conn, "last_collect_at"),
            "articles": conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
            "failing_feeds": [dict(r) for r in conn.execute(
                "SELECT source, category, url, last_success_at, last_error FROM feed_status WHERE last_error IS NOT NULL")],
            "summaries": summarizer.stats(),
        }


@router.get("/weather")
def get_weather():
    data = weather.get_weather()
    if data is None:
        raise HTTPException(503, "날씨 정보를 불러오지 못했어요")
    return data


# ── 기사 ────────────────────────────────────────────────

@router.get("/recommendations")
def recommendations():
    ctx = _context()
    picks = recommender.recommend(ctx)
    explains = [recommender.explain(ctx, p) for p in picks]   # 추천에 쓴 점수 그대로 설명
    # 추천된 기사만 AI 요약 대기열에 넣음 (백그라운드에서 처리, 화면은 기다리지 않음)
    queued = set(summarizer.enqueue([p["article"]["id"] for p in picks]))
    items = []
    for p, why in zip(picks, explains):
        a = recommender.clean(dict(p["article"]))
        if a["id"] in queued:
            a["summary_status"] = "pending"
        items.append({**_public(a), "reason": p["reason"], "explain": why})
    return {
        "mode": ctx.mode,   # latest: 주요 뉴스 / vector: 벡터 유사도 / keyword: 모델 없을 때 키워드
        "items": items,
    }


@router.get("/taste")
def taste():
    """내 취향: 관심사를 모델이 어떤 단어로 이해했는지, 관심 없는 것이 무엇을 막았는지, 오늘의 기사 지도."""
    ctx = _context()
    picks = recommender.recommend(ctx)
    return recommender.taste(ctx, {p["idx"] for p in picks})


@router.get("/summaries")
def summaries(ids: str):
    """요약 준비 중인 기사들의 현재 요약 상태 (화면이 몇 초마다 확인)."""
    wanted = [int(x) for x in ids.split(",") if x.strip().isdigit()][:20]
    if not wanted:
        return {"items": []}
    with db.session() as conn:
        rows = conn.execute(
            f"""SELECT id, rss_summary, ai_summary, summary_status FROM articles
                WHERE id IN ({",".join("?" * len(wanted))})""", wanted).fetchall()
    out = []
    for r in rows:
        p = _public({**dict(r), "source": "", "title": "", "link": "", "published_at": "", "has_time": 0,
                     "categories": [], "liked": 0, "read": 0})
        out.append({k: p[k] for k in ("id", "summary", "summary_kind", "summary_status")})
    return {"items": out}


@router.get("/articles")
def articles(category: str = "전체", sources: str | None = None):
    wanted = set(sources.split(",")) if sources else None
    with db.session() as conn:
        if category == "전체":
            rows = _rows(conn)
        else:
            rows = _rows(conn, " AND a.id IN (SELECT article_id FROM article_categories WHERE category = :cat)", cat=category)
    if wanted is not None:
        rows = [r for r in rows if r["source"] in wanted]
    # 언론사당 최신 5건, 언론사를 번갈아 섞기 (가장 최근 기사가 있는 언론사부터)
    groups: dict[str, list[dict]] = {}
    for r in rows:  # 이미 최신순
        g = groups.setdefault(r["source"], [])
        if len(g) < PER_SOURCE_IN_TAB:
            g.append(r)
    out = [g[i] for i in range(PER_SOURCE_IN_TAB) for g in groups.values() if i < len(g)]
    return {"items": [_public(a) for a in out]}


# ── 사용자 ──────────────────────────────────────────────

class Profile(BaseModel):
    onboarded: bool = True
    interests: list[str] = Field(default_factory=list, max_length=50)
    dislikes: list[str] = Field(default_factory=list, max_length=50)


@router.get("/profile")
def get_profile():
    with db.session() as conn:
        likes, dislikes = _interests(conn)
        onboarded = conn.execute("SELECT onboarded FROM users WHERE id = ?", (USER,)).fetchone()["onboarded"]
        return {"onboarded": bool(onboarded), "interests": likes, "dislikes": dislikes}


@router.put("/profile")
def put_profile(p: Profile):
    clean = lambda xs: list(dict.fromkeys(x.strip()[:30] for x in xs if x.strip()))
    with db.session() as conn:
        conn.execute("UPDATE users SET onboarded = ? WHERE id = ?", (int(p.onboarded), USER))
        conn.execute("DELETE FROM interests WHERE user_id = ?", (USER,))
        conn.executemany("INSERT INTO interests (user_id, text, kind) VALUES (?, ?, 'like')", [(USER, t) for t in clean(p.interests)])
        conn.executemany("INSERT INTO interests (user_id, text, kind) VALUES (?, ?, 'dislike')", [(USER, t) for t in clean(p.dislikes)])
    embedder.embed_interests(USER)  # 관심사 글을 벡터로 (모델이 없으면 건너뜀 → 키워드 추천)
    return get_profile()


EventType = Literal["impression", "click", "like", "unlike", "dislike", "undislike", "hide", "unhide"]

# 행동 → 기사 상태 변경
STATE_CHANGES = {
    "click": {"read": 1},
    "like": {"liked": 1, "disliked": 0},
    "unlike": {"liked": 0},
    "dislike": {"disliked": 1, "liked": 0},
    "undislike": {"disliked": 0},
    "hide": {"hidden": 1},
    "unhide": {"hidden": 0},
}


class Event(BaseModel):
    article_id: int
    type: EventType


class Events(BaseModel):
    events: list[Event] = Field(max_length=100)


@router.post("/events")
def post_events(body: Events):
    with db.session() as conn:
        for e in body.events:
            if not conn.execute("SELECT 1 FROM articles WHERE id = ?", (e.article_id,)).fetchone():
                continue
            conn.execute("INSERT INTO events (user_id, article_id, type) VALUES (?, ?, ?)", (USER, e.article_id, e.type))
            change = STATE_CHANGES.get(e.type)
            if change:
                cols = ", ".join(change)
                conn.execute(
                    f"INSERT INTO article_state (user_id, article_id, {cols}) VALUES (?, ?, {', '.join('?' * len(change))}) "
                    f"ON CONFLICT(user_id, article_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in change)}",
                    (USER, e.article_id, *change.values()),
                )
    return {"ok": True}


@router.delete("/me")
def delete_me():
    """내 기록 전체 삭제: 관심사, 행동 기록, 기사 상태."""
    with db.session() as conn:
        conn.execute("DELETE FROM interests WHERE user_id = ?", (USER,))
        conn.execute("DELETE FROM events WHERE user_id = ?", (USER,))
        conn.execute("DELETE FROM article_state WHERE user_id = ?", (USER,))
        conn.execute("UPDATE users SET onboarded = 0 WHERE id = ?", (USER,))
    return {"ok": True}
