"""GitHub Actions에서 1시간마다 실행하는 데이터 만들기 (PLAN.md 7장).

① 지난번 배포한 데이터 파일을 받아 임시 DB에 넣음 → DB를 따로 운영하지 않음
② 15개 언론사 RSS 수집·정리 → 새 기사만 32비트 e5-small 벡터 (Hugging Face API와 같은 벡터)
③ 사용자별 관심사·행동 기록(Blobs에서 내보낸 파일)으로 추천을 미리 계산해, 그 기사만 AI 요약
④ 데이터 파일 쓰기: articles.json · vectors.bin · suggested.json · meta.json · state.json

수집·벡터·본문 추출·요약·추천은 app/ 의 모듈을 그대로 쓴다.

실행 예:
  python -m pipeline.build --prev https://내사이트.netlify.app --users users.json --out static/data
  python -m pipeline.build --prev static/data --out static/data      # 로컬: 폴더를 이전 상태로
  python -m pipeline.build --out static/data --no-summaries            # 처음부터, 요약 없이
"""
import argparse
import hashlib
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

# app 모듈을 불러오기 전에: 32비트 모델(HF API와 같은 벡터), 임시 DB
os.environ.setdefault("EMBED_VARIANT", "fp32")
_TMP = Path(tempfile.mkdtemp(prefix="briefing-"))
os.environ["BRIEFING_DB"] = str(_TMP / "briefing.db")

import httpx  # noqa: E402
import numpy as np  # noqa: E402

from app import collector, db, embedder, recommender, summarizer  # noqa: E402
from app.collector import KST  # noqa: E402
from app.interests import SUGGESTED_INTERESTS  # noqa: E402

log = logging.getLogger("pipeline")

EXPORT_HOURS = 72        # 화면에 보내는 기사 (추천은 그중 최근 48시간)
SEEN_DAYS = 7            # 이보다 오래된 기사 주소는 잊음 (그 전까지는 다시 수집해도 새 기사로 보지 않음)
SUMMARY_TEXT_CHARS = 300 # 화면에 보내는 RSS 요약문 길이
SEEN_SOURCE = "__seen__" # 지난 기사 자리 표시
DIM = embedder.DIM
VEC_DTYPE = np.dtype([("scale", "<f4"), ("q", "i1", (DIM,))])   # 기사당 388바이트


def article_key(link: str) -> str:
    """화면·행동 기록에서 쓰는 기사 ID. 주소에서 만들므로 실행이 바뀌어도 같음."""
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:12]


# ── 벡터 파일: 기사마다 (배율 float32, int8 × 384) ───────────────

def pack_vectors(V: np.ndarray) -> bytes:
    rec = np.zeros(len(V), dtype=VEC_DTYPE)
    if len(V):
        scale = np.abs(V).max(1) / 127.0
        scale[scale == 0] = 1.0
        rec["scale"] = scale
        rec["q"] = np.clip(np.round(V / scale[:, None]), -127, 127).astype(np.int8)
    return rec.tobytes()


def unpack_vectors(b: bytes) -> np.ndarray:
    rec = np.frombuffer(b, dtype=VEC_DTYPE)
    return rec["q"].astype(np.float32) * rec["scale"][:, None]


# ── ① 이전 상태 불러오기 ──────────────────────────────────────

def _read(prev: str | None, name: str, binary: bool = False):
    if not prev:
        return None
    try:
        if prev.startswith("http"):
            res = httpx.get(f"{prev.rstrip('/')}/data/{name}", timeout=30, follow_redirects=True)
            if res.status_code != 200:
                return None
            return res.content if binary else res.json()
        path = Path(prev) / name
        if not path.exists():
            return None
        return path.read_bytes() if binary else json.loads(path.read_text(encoding="utf-8"))
    except (httpx.HTTPError, ValueError) as e:
        log.warning("이전 파일 %s를 읽지 못함: %s", name, e)
        return None


def restore(prev: str | None) -> dict:
    """이전 데이터 파일을 임시 DB에 넣는다. 처음이면 빈 상태."""
    db.init()
    articles = (_read(prev, "articles.json") or {}).get("articles", [])
    vec_bytes = _read(prev, "vectors.bin", binary=True)
    vectors = unpack_vectors(vec_bytes) if vec_bytes else np.zeros((0, DIM), np.float32)
    meta = _read(prev, "meta.json") or {}
    state = _read(prev, "state.json") or {}
    suggested = _read(prev, "suggested.json") or {}
    if len(vectors) != len(articles):
        log.warning("기사 %d건과 벡터 %d건이 맞지 않아 벡터를 다시 만듦", len(articles), len(vectors))
        vectors = None

    with db.session() as conn:
        for i, a in enumerate(articles):
            cur = conn.execute(
                """INSERT OR IGNORE INTO articles (link, source, title, rss_summary, published_at, has_time, fetched_at,
                       ai_summary, summary_status, summary_source, summary_attempts, embedding)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (a["link"], a["source"], a["title"], a.get("rss_summary"), a["published_at"], int(a["has_time"]),
                 a.get("fetched_at") or a["published_at"], a.get("ai_summary"), a.get("summary_status", "none"),
                 a.get("summary_source"), a.get("summary_attempts", 0),
                 embedder.to_blob(vectors[i]) if vectors is not None else None))
            for c in a.get("categories", []):
                conn.execute("INSERT OR IGNORE INTO article_categories (article_id, category) VALUES (?, ?)", (cur.lastrowid, c))
        # 지난 기사: 다시 수집돼도 새 기사로 보지 않도록 자리만 남김 (벡터 자리는 b'' → 벡터를 만들지 않음)
        for link, published in state.get("seen", {}).items():
            conn.execute("""INSERT OR IGNORE INTO articles (link, source, title, published_at, has_time, fetched_at, embedding)
                            VALUES (?, ?, '', ?, 1, ?, x'')""", (link, SEEN_SOURCE, published, published))
        for f in meta.get("feed_status", []):
            conn.execute("""INSERT OR REPLACE INTO feed_status (url, source, category, last_success_at, last_error, last_count)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                         (f["url"], f["source"], f["category"], f.get("last_success_at"), f.get("last_error"), f.get("last_count")))
        if meta.get("updated_at"):
            db.set_meta(conn, "last_collect_at", meta["updated_at"])
        for day, n in meta.get("summary_counts", {}).items():
            db.set_meta(conn, f"summary_count:{day}", str(n))
    log.info("이전 상태: 기사 %d건, 지난 기사 주소 %d건", len(articles), len(state.get("seen", {})))
    return {"suggested": {it["text"]: it["vector"] for it in suggested.get("items", [])}}


# ── ③ 사용자별 추천 후보 → 요약 ───────────────────────────────

def _load_export_rows(conn, since_iso: str) -> list[dict]:
    rows = conn.execute(
        """SELECT a.*, COALESCE(GROUP_CONCAT(DISTINCT c.category), '') AS cats FROM articles a
           LEFT JOIN article_categories c ON c.article_id = a.id
           WHERE a.source != ? AND a.published_at >= ? AND a.embedding IS NOT NULL AND length(a.embedding) > 0
           GROUP BY a.id ORDER BY a.published_at DESC, a.id DESC""", (SEEN_SOURCE, since_iso)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["categories"] = sorted(c for c in d.pop("cats").split(",") if c)
        d["vec"] = embedder.from_blob(d.pop("embedding"))
        d["key"] = article_key(d["link"])
        out.append(d)
    return out


def _user_state(events: list[dict]) -> dict:
    """행동 기록 → 기사별 현재 상태 (나중 기록이 이김). liked/disliked는 최근 순."""
    st: dict[str, dict] = {}
    order = []
    for e in sorted(events, key=lambda e: e.get("at", "")):
        k, t = e.get("article_id"), e.get("type")
        s = st.setdefault(k, {"liked": False, "disliked": False, "hidden": False, "read": False})
        if t == "click":
            s["read"] = True
        elif t == "like":
            s.update(liked=True, disliked=False)
        elif t == "unlike":
            s["liked"] = False
        elif t == "dislike":
            s.update(disliked=True, liked=False)
        elif t == "undislike":
            s["disliked"] = False
        elif t == "hide":
            s["hidden"] = True
        elif t == "unhide":
            s["hidden"] = False
        else:
            continue
        order.append(k)
    recent = list(dict.fromkeys(reversed(order)))
    return {"state": st, "recent": recent}


def recommend_for_users(rows: list[dict], users: dict, interest_vectors: dict, suggested: dict) -> list[int]:
    """사용자마다 추천을 계산해 요약할 기사 id를 모음 (사용자들을 번갈아 가며 → 상한을 고르게 씀)."""
    by_key = {r["key"]: r for r in rows}
    missing_texts = set()

    def vec_for(text: str):
        v = suggested.get(text) or interest_vectors.get(text)
        if v is None:
            missing_texts.add(text)
            return None
        return np.asarray(v, np.float64)   # 브라우저가 JSON에서 읽는 값(64비트)과 같게

    people = list(users.items()) + [("__guest__", {"profile": {}, "events": []})]   # 관심사 없는 첫 화면도 미리 요약
    # 아직 벡터가 없는 관심사는 여기서 32비트 모델로 만듦 (HF API와 같은 벡터)
    for _, u in people:
        for t in (u.get("profile") or {}).get("interests", []) + (u.get("profile") or {}).get("dislikes", []):
            vec_for(t)
    if missing_texts:
        texts = sorted(missing_texts)
        for t, v in zip(texts, embedder.encode(texts, "query")):
            interest_vectors[t] = v.tolist()
        log.info("벡터가 없던 관심사 %d개를 파이프라인에서 만듦", len(texts))

    per_user = []
    for uid, u in people:
        profile = u.get("profile") or {}
        st = _user_state(u.get("events", []))
        pool = []
        for r in rows:
            s = st["state"].get(r["key"], {})
            if s.get("hidden") or s.get("disliked"):
                continue
            pool.append({**r, "liked": s.get("liked", False), "read": s.get("read", False)})
        liked = [(by_key[k]["title"], by_key[k]["vec"]) for k in st["recent"]
                 if k in by_key and st["state"][k]["liked"]][:50]
        disliked = [by_key[k]["vec"] for k in st["recent"] if k in by_key and st["state"][k]["disliked"]][:50]
        likes = [(t, vec_for(t)) for t in profile.get("interests", [])]
        dislikes = [(t, vec_for(t)) for t in profile.get("dislikes", [])]
        ctx = recommender.build_context(pool, likes, dislikes, liked, disliked)
        per_user.append([p["article"]["id"] for p in recommender.recommend(ctx)])
    ordered = []
    for rank in range(recommender.LIMIT):
        for ids in per_user:
            if rank < len(ids) and ids[rank] not in ordered:
                ordered.append(ids[rank])
    log.info("추천 후보: 사용자 %d명(+첫 화면), 요약 후보 %d건", len(users), len(ordered))
    return ordered


# ── ④ 데이터 파일 쓰기 ────────────────────────────────────────

def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def export(out: Path, prev_suggested: dict, now: datetime) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    since = (now - timedelta(hours=EXPORT_HOURS)).isoformat(timespec="seconds")
    with db.session() as conn:
        rows = _load_export_rows(conn, since)
        seen_since = (now - timedelta(days=SEEN_DAYS)).isoformat(timespec="seconds")
        seen = {r["link"]: r["published_at"] for r in conn.execute(
            "SELECT link, published_at FROM articles WHERE published_at < ? AND published_at >= ?", (since, seen_since))}
        feeds = [dict(r) for r in conn.execute(
            "SELECT url, source, category, last_success_at, last_error, last_count FROM feed_status ORDER BY source, category")]
        updated = db.get_meta(conn, "last_collect_at")
        counts = {k.split(":", 1)[1]: int(v) for k, v in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'summary_count:%'")}

    # 같은 사건을 다룬 다른 언론사 수 (브라우저와 파이프라인이 같은 값을 쓰도록 여기서 한 번 계산)
    V = np.vstack([r["vec"] for r in rows]) if rows else np.zeros((0, DIM), np.float32)
    covs = recommender._coverage(rows, V.astype(np.float64)) if rows else []

    articles = [{
        "id": r["key"], "source": r["source"], "title": r["title"], "link": r["link"],
        "published_at": r["published_at"], "has_time": bool(r["has_time"]), "fetched_at": r["fetched_at"],
        "categories": r["categories"], "rss_summary": (r["rss_summary"] or "")[:SUMMARY_TEXT_CHARS] or None,
        "ai_summary": r["ai_summary"], "summary_status": r["summary_status"], "summary_source": r["summary_source"],
        "summary_attempts": r["summary_attempts"], "cov": int(c),
    } for r, c in zip(rows, covs)]
    write_json(out / "articles.json", {"generated_at": now.isoformat(timespec="seconds"), "articles": articles})
    (out / "vectors.bin").write_bytes(pack_vectors(V))

    # 추천 관심사 벡터: 목록에 새로 생긴 것만 만듦
    todo = [t for t in SUGGESTED_INTERESTS if t not in prev_suggested]
    if todo:
        for t, v in zip(todo, embedder.encode(todo, "query")):
            prev_suggested[t] = [round(float(x), 6) for x in v]
    write_json(out / "suggested.json", {
        "model": "intfloat/multilingual-e5-small", "prefix": "query: ",
        "items": [{"text": t, "vector": prev_suggested[t]} for t in SUGGESTED_INTERESTS],
    })

    cfg = collector.load_feeds()
    keep_days = {(now - timedelta(days=d)).date().isoformat() for d in range(3)}
    meta = {
        "updated_at": updated,
        "generated_at": now.isoformat(timespec="seconds"),
        "sources": list(dict.fromkeys(f["source"] for f in cfg["feeds"])),
        "categories": ["전체", *cfg["categories"]],
        "suggested_interests": SUGGESTED_INTERESTS[:12],
        "feeds_failed": sum(1 for f in feeds if f["last_error"]),
        "feed_status": feeds,
        "summary_counts": {d: n for d, n in counts.items() if d in keep_days},
        "summary_daily_limit": summarizer._daily_limit(),
        "export_hours": EXPORT_HOURS,
        "pool_hours": recommender.RECENT_HOURS,
        "model": "intfloat/multilingual-e5-small",
        "embed_variant": embedder.VARIANT,
        "article_count": len(articles),
    }
    write_json(out / "meta.json", meta)
    write_json(out / "state.json", {"seen": seen})
    sizes = {p.name: p.stat().st_size for p in out.iterdir() if p.is_file()}
    log.info("데이터 파일: %s", ", ".join(f"{k} {v / 1024:.0f}KB" for k, v in sorted(sizes.items())))
    return {"articles": len(articles), "seen": len(seen), "sizes": sizes}


def main() -> None:
    ap = argparse.ArgumentParser(description="아침 브리핑 데이터 파일 만들기")
    ap.add_argument("--prev", help="이전 상태: 사이트 주소(https://…) 또는 폴더")
    ap.add_argument("--users", help="Blobs에서 내보낸 사용자 파일 (pipeline/blobs.mjs export)")
    ap.add_argument("--out", required=True, help="데이터 파일을 쓸 폴더 (예: static/data)")
    ap.add_argument("--no-summaries", action="store_true", help="AI 요약을 하지 않음 (시험용)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "openai", "trafilatura"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    t0 = time.time()
    timing = {}

    prev = restore(args.prev)
    timing["restore"] = time.time() - t0

    t = time.time()
    summary = collector.collect()
    timing["collect"] = time.time() - t

    t = time.time()
    embedder.download()          # Actions에서는 캐시가 있으면 건너뜀
    n_embedded = embedder.embed_new_articles()
    timing["embed"] = time.time() - t

    now = datetime.now(KST)
    t = time.time()
    n_summarized = 0
    if not args.no_summaries and summarizer.enabled():
        users_file = json.loads(Path(args.users).read_text(encoding="utf-8")) if args.users else {}
        with db.session() as conn:
            rows = _load_export_rows(conn, (now - timedelta(hours=EXPORT_HOURS)).isoformat(timespec="seconds"))
        V = np.vstack([r["vec"] for r in rows]) if rows else np.zeros((0, DIM), np.float32)
        for r, c in zip(rows, recommender._coverage(rows, V.astype(np.float64)) if rows else []):
            r["cov"] = int(c)
        # 브라우저는 vectors.bin(8비트)에서 되돌린 벡터를 쓰므로, 추천도 같은 벡터로 계산 (결과를 맞추기 위해)
        for r, v in zip(rows, unpack_vectors(pack_vectors(V))):
            r["vec"] = v
        ids = recommend_for_users(rows, users_file.get("users", {}), users_file.get("interest_vectors", {}),
                                  {k: v for k, v in prev["suggested"].items()})
        n_summarized = len(summarizer.summarize_now(ids))
    elif not args.no_summaries:
        log.warning("OPENAI_API_KEY가 없어 AI 요약을 건너뜀")
    timing["summaries"] = time.time() - t

    t = time.time()
    result = export(Path(args.out), prev["suggested"], now)
    timing["export"] = time.time() - t

    log.info("완료: 수집 %s / 새 벡터 %d건 / 요약 %d건 / 기사 %d건 / 단계별 시간 %s / 전체 %.0f초",
             {k: summary[k] for k in ("feeds_ok", "feeds_failed", "new_articles")}, n_embedded, n_summarized,
             result["articles"], {k: round(v, 1) for k, v in timing.items()}, time.time() - t0)


if __name__ == "__main__":
    main()
