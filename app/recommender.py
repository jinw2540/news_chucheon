"""추천 목록 만들기와 추천 이유 설명 (PLAN.md 4장).

3단계: e5-small 벡터 유사도 추천
- 관심사마다 따로 점수를 매기고(z점수), 관심사를 번갈아 뽑아 한쪽으로 쏠리지 않게 함
- 좋아요가 3개 이상이면 '좋아요한 기사들의 평균 벡터'도 관심사처럼 사용 (콜드 스타트 C단계)
- 관심 없는 것으로 적은 주제, 별로라고 한 기사와 가까운 기사는 점수를 깎음
- 벡터를 쓸 수 없으면(모델 없음) 2단계 키워드 방식으로 대체

설명(왜 이 기사?·내 취향)은 추천에 실제로 쓴 점수(Context)를 그대로 보여 준다.
5단계에서 랭킹 신경망으로 점수 계산을 바꾸더라도 관심사별 점수는 입력으로 남겨 설명에 쓴다.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from .collector import KST

RECENT_HOURS = 48
LIMIT = 10
MAX_PER_SOURCE = 2
EXPLORE_SLOTS = (3, 7)      # 10개 중 4번째·8번째 자리는 새로운 주제 탐색
SIMILAR_TITLE = 0.45        # 제목 글자 겹침이 이 이상이면 같은 사건
SIMILAR_VECTOR = 0.90       # 기사 벡터가 이만큼 가까우면 같은 사건 (실제 기사로 측정해 정함)
MAJOR_SOURCES = 3           # 이 이상의 다른 언론사가 함께 다루면 '주요 뉴스'
MATCH_Z = 3.0               # 관심사 z점수가 이 이상이어야 '관심사와 관련'으로 봄 (실제 기사로 측정해 정함)
TITLE_KEYWORD_BONUS = 1.0   # 제목에 관심사 단어가 그대로 있으면 가산
DISLIKE_Z = 2.0             # 관심 없는 주제와 이만큼 가까우면 크게 감점
MIN_LIKES_FOR_BEHAVIOR = 3
# 단어 기준 유사 피드백: 관심사 단어가 실제로 들어간 기사들의 평균 벡터를 반 섞어 '주제 묶음'으로 넓힘.
# 처음엔 벡터로 가까운 기사 20개를 썼지만, 모델이 잘 모르는 단어(예: 이더리움)는 엉뚱한 이웃을 끌어와
# 관련 0%가 됨 → 단어가 든 기사를 기준점으로 바꾸니 9개 관심사 모두 정확도 상승 (이더리움 0→100%)
FEEDBACK_MIX = 0.5
MIN_ANCHORS = 2             # 단어가 든 기사가 이보다 적으면 넓히지 않고 기준을 높임
UNANCHORED_Z = 4.0
SIMILAR_LIKED = 0.85        # 설명에 "좋아요한 OO과 비슷해요"를 보여 줄 최소 유사도
BEHAVIOR_KEY = "좋아요 취향"

LATEST = {"kind": "latest", "label": "최신 주요 뉴스"}
EXPLORE = {"kind": "explore", "label": "새로운 주제 둘러보기"}


def _norm(s: str) -> str:
    return "".join(s.lower().split())


def _bigrams(s: str) -> set[str]:
    s = _norm(s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _zscore(x: np.ndarray) -> np.ndarray:
    std = x.std()
    return (x - x.mean()) / std if std > 0 else np.zeros_like(x)


# ── 점수 계산 (추천과 설명이 함께 씀) ───────────────────────

@dataclass
class Group:
    """관심사 하나(또는 좋아요 취향)에 대한 오늘 기사들의 점수."""
    key: str                 # 관심사 글, 또는 BEHAVIOR_KEY
    method: str              # vector: 벡터 z점수 / keyword: 글자 일치 점수 (벡터가 아직 없는 관심사)
    reason: dict
    z: np.ndarray            # 관심 점수 (z점수 + 제목 키워드 가산)
    need: float              # 이 이상이어야 관련 있음
    title_hit: np.ndarray    # 제목에 관심사 단어가 그대로 있는지
    ok: np.ndarray           # 추천 후보가 될 수 있는지
    score: np.ndarray        # 정렬용 = z + 감점 + 최신성


@dataclass
class Context:
    pool: list[dict]
    mode: str                                    # latest / vector / keyword
    groups: list[Group] = field(default_factory=list)
    penalty: np.ndarray | None = None
    blocked_by: list[tuple[str, np.ndarray]] = field(default_factory=list)   # (관심 없는 것, 막힌 기사)
    M: np.ndarray | None = None
    liked: list[tuple[str, np.ndarray]] = field(default_factory=list)       # (제목, 벡터) 최근 순


def _coverage(pool: list[dict], M: np.ndarray | None) -> np.ndarray:
    """기사마다 같은 사건을 다룬 '다른 언론사' 수. 많을수록 그날의 주요 뉴스.
    파이프라인이 미리 계산해 둔 값('cov')이 있으면 그것을 씀 (브라우저 JS 버전과 같은 값을 쓰기 위해)."""
    if pool and all("cov" in a for a in pool):
        return np.array([a["cov"] for a in pool], dtype=int)
    if M is None:
        return np.zeros(len(pool), dtype=int)
    same = (M @ M.T) >= SIMILAR_VECTOR
    src = np.array([a["source"] for a in pool])
    return np.array([len(set(src[same[i]]) - {src[i]}) for i in range(len(pool))])


def _anchors(text: str, docs: list[str]) -> list[int]:
    """관심사 단어가 들어간 기사. 구 전체가 없으면 구를 이루는 단어가 모두 든 기사."""
    whole = _norm(text)
    hits = [i for i, d in enumerate(docs) if whole in d]
    words = [_norm(w) for w in text.split() if len(w) >= 2]
    if len(hits) < MIN_ANCHORS and len(words) > 1:
        hits = [i for i, d in enumerate(docs) if all(w in d for w in words)]
    return hits


def _expand(M: np.ndarray, v: np.ndarray, text: str, docs: list[str], z_needed: float) -> tuple[np.ndarray, float]:
    """단어 기준 유사 피드백. (넓힌 벡터, 필요한 z점수)"""
    hits = _anchors(text, docs)
    if len(hits) < MIN_ANCHORS:
        return v, max(z_needed, UNANCHORED_Z)   # 근거가 약하니 더 확실한 기사만
    u = FEEDBACK_MIX * v + (1 - FEEDBACK_MIX) * M[hits].mean(0)
    return u / np.linalg.norm(u), z_needed


def build_context(articles: list[dict], likes: list[tuple], dislikes: list[tuple],
                  liked: list[tuple], disliked_vecs: list[np.ndarray], now: datetime | None = None) -> Context:
    """articles: 최근 기사(숨김·별로 제외, 'vec' 포함 가능). likes/dislikes: [(글, 벡터|None)]
    liked: [(제목, 벡터)] 좋아요한 기사 (최근 순). now: 기준 시각 (일치 시험에서 고정)"""
    now = now or datetime.now(KST)
    since = now - timedelta(hours=RECENT_HOURS)
    pool = [a for a in articles if datetime.fromisoformat(a["published_at"]) >= since]
    vec_ready = bool(pool) and all(a.get("vec") is not None for a in pool)
    if vec_ready:   # JS(64비트 실수)와 같은 정밀도로 계산
        for a in pool:
            a["vec"] = np.asarray(a["vec"], np.float64)
    M = np.vstack([a["vec"] for a in pool]) if vec_ready else None
    for a, cov in zip(pool, _coverage(pool, M)):
        a["_age_h"] = (now - datetime.fromisoformat(a["published_at"])).total_seconds() / 3600
        a["_bigrams"] = _bigrams(a["title"])
        a["_cov"] = int(cov)

    personal = bool(likes) or len(liked) >= MIN_LIKES_FOR_BEHAVIOR
    if not personal or not pool:
        return Context(pool=pool, mode="latest", M=M, liked=liked, penalty=np.zeros(len(pool)))

    ctx = Context(pool=pool, mode="vector" if vec_ready else "keyword", M=M, liked=liked)
    titles = [_norm(a["title"]) for a in pool]
    summaries = [_norm(a.get("rss_summary") or "") for a in pool]
    docs = [t + s for t, s in zip(titles, summaries)]
    ctx.penalty = np.zeros(len(pool))
    fresh_vec = -np.array([a["_age_h"] for a in pool]) / RECENT_HOURS  # 최신일수록 덜 깎음 (최대 -1)
    fresh_kw = -np.array([a["_age_h"] for a in pool]) / 12

    def keyword_scores(text):
        """벡터가 없을 때: 관심사 글자가 제목(2점)·요약문(1.2점)에 있는지."""
        k = _norm(text)
        title_hit = np.array([k in t for t in titles])
        base = np.where(title_hit, 2.0, np.array([1.2 if k in s_ else 0.0 for s_ in summaries]))
        return base, title_hit

    # 관심 없는 것
    for text, v in dislikes:
        if vec_ready and v is not None:
            u, need = _expand(M, np.asarray(v, np.float64), text, docs, DISLIKE_Z)
            hit = (_zscore(M @ u) >= need) | np.array([_norm(text) in t for t in titles])
        else:
            hit = np.array([_norm(text) in d for d in docs])
        ctx.blocked_by.append((text, hit))
        ctx.penalty -= 3.0 * hit
    if vec_ready and len(disliked_vecs) >= 2:
        u = np.mean(np.asarray(disliked_vecs, np.float64), axis=0)
        hit = _zscore(M @ (u / np.linalg.norm(u))) >= MATCH_Z
        ctx.blocked_by.append(("별로예요 누른 기사", hit))
        ctx.penalty -= 2.0 * hit

    # 관심사: 벡터가 있으면 벡터, 아직 없으면(직접 입력 직후 등) 키워드
    for text, v in likes:
        reason = {"kind": "interest", "label": f"관심사 · {text}"}
        if vec_ready and v is not None:
            u, need = _expand(M, np.asarray(v, np.float64), text, docs, MATCH_Z)
            title_hit = np.array([_norm(text) in t for t in titles])
            z = _zscore(M @ u) + TITLE_KEYWORD_BONUS * title_hit
            ctx.groups.append(Group(text, "vector", reason, z, need, title_hit, z >= need, z + ctx.penalty + fresh_vec))
        else:
            base, title_hit = keyword_scores(text)
            ctx.groups.append(Group(text, "keyword", reason, base, 1.0, title_hit, base > 0, base + ctx.penalty + fresh_kw))
    if vec_ready and len(liked) >= MIN_LIKES_FOR_BEHAVIOR:
        vecs = np.asarray([v for _, v in liked], np.float64)
        w = 0.9 ** np.arange(len(vecs))          # 최근 좋아요일수록 크게
        u = (vecs * w[:, None]).sum(0)
        z = _zscore(M @ (u / np.linalg.norm(u)))
        not_liked = np.array([not a.get("liked") for a in pool])  # 좋아요한 기사 자신은 다시 추천하지 않음
        ctx.groups.append(Group(BEHAVIOR_KEY, "vector", {"kind": "interest", "label": "좋아요한 기사와 비슷해요"},
                                z, MATCH_Z, np.zeros(len(pool), bool), (z >= MATCH_Z) & not_liked,
                                z + ctx.penalty + fresh_vec))
    return ctx


# ── 기사 고르기 ──────────────────────────────────────────

class _Picker:
    """언론사당 개수 제한과 같은 사건 중복을 막으면서 기사를 고른다."""

    def __init__(self):
        self.picks: list[dict] = []
        self.per_source: dict[str, int] = {}

    def can(self, a: dict, cap: int = MAX_PER_SOURCE) -> bool:
        if self.per_source.get(a["source"], 0) >= cap:
            return False
        for p in self.picks:
            b = p["article"]
            if b["id"] == a["id"]:
                return False
            if len(a["_bigrams"] & b["_bigrams"]) / max(len(a["_bigrams"] | b["_bigrams"]), 1) >= SIMILAR_TITLE:
                return False
            if a.get("vec") is not None and b.get("vec") is not None and float(a["vec"] @ b["vec"]) >= SIMILAR_VECTOR:
                return False
        return True

    def take(self, a: dict, reason: dict, idx: int, at: int | None = None) -> None:
        item = {"article": a, "reason": reason, "idx": idx}
        self.picks.insert(len(self.picks) if at is None else min(at, len(self.picks)), item)
        self.per_source[a["source"]] = self.per_source.get(a["source"], 0) + 1


def _latest_reason(a: dict) -> dict:
    if a["_cov"] >= MAJOR_SOURCES:
        return {"kind": "latest", "label": f"주요 뉴스 · {a['_cov'] + 1}개 언론사"}
    return LATEST


def recommend(ctx: Context) -> list[dict]:
    """돌려주는 값: [{article, reason, idx(=pool 안 위치)}]"""
    pool = ctx.pool
    index = {id(a): i for i, a in enumerate(pool)}
    # 주요도: 여러 언론사가 다룬 기사일수록, 최신일수록 앞으로 (모델이 없으면 최신순과 같음)
    latest = sorted(pool, key=lambda a: -min(a["_cov"], 10) + a["_age_h"] / 4)   # sorted는 안정 정렬
    picker = _Picker()

    if ctx.mode == "latest":
        # 콜드 스타트 A: 주요 뉴스 + 언론사 균형 (언론사당 1개)
        for a in latest:
            if len(picker.picks) < LIMIT and picker.can(a, 1):
                picker.take(a, _latest_reason(a), index[id(a)])
        return picker.picks

    # 관심사를 번갈아 가며 가장 점수가 높은 기사를 하나씩
    personal_slots = LIMIT - len(EXPLORE_SLOTS)
    queues = [(g.reason, [int(i) for i in np.argsort(-g.score, kind="stable") if g.ok[i]]) for g in ctx.groups]
    pos = [0] * len(queues)
    while len(picker.picks) < personal_slots:
        progressed = False
        for g, (reason, order) in enumerate(queues):
            while pos[g] < len(order):
                i = order[pos[g]]
                pos[g] += 1
                if picker.can(pool[i]):
                    picker.take(pool[i], reason, i)
                    progressed = True
                    break
            if len(picker.picks) >= personal_slots:
                break
        if not progressed:
            break

    # 관련 기사가 모자라면 주요 뉴스로 채움 (관심 없는 주제는 제외)
    blocked = {pool[i]["id"] for i in np.where(ctx.penalty < 0)[0]}
    for a in latest:
        if len(picker.picks) >= personal_slots:
            break
        if a["id"] not in blocked and picker.can(a):
            picker.take(a, _latest_reason(a), index[id(a)])

    # 탐색 자리: 지금까지 고른 기사에 없는 분야의 주요 뉴스에서
    used = {c for p in picker.picks for c in p["article"]["categories"]}
    candidates = [a for a in latest if a["categories"] and not set(a["categories"]) & used] + latest
    slots = iter(EXPLORE_SLOTS)
    slot = next(slots)
    for a in candidates:
        if a["id"] not in blocked and picker.can(a):
            reason = dict(EXPLORE, category=next((c for c in a["categories"] if c not in used), None))
            picker.take(a, reason, index[id(a)], at=slot)   # 바로 넣어서 다음 탐색 기사와 중복되지 않게
            slot = next(slots, None)
            if slot is None:
                break
    return picker.picks[:LIMIT]


# ── 설명: 왜 이 기사? ────────────────────────────────────

def _top_percent(values: np.ndarray, i: int) -> int:
    """오늘 기사 중 상위 몇 %인지 (1~100)."""
    higher = int((values > values[i]).sum())
    return max(1, math.ceil(100 * (higher + 1) / len(values)))


def explain(ctx: Context, pick: dict) -> dict:
    """추천에 실제로 쓴 점수로 만든 설명."""
    i, a, reason = pick["idx"], pick["article"], pick["reason"]
    bars = []
    for g in ctx.groups:
        # 막대 길이: z점수 0 이하면 빈 막대, 관련 기준(need)에 닿으면 가득 (키워드 방식은 점수/2)
        full = g.need if g.method == "vector" else 2.0
        bars.append({
            "label": "좋아요 취향" if g.key == BEHAVIOR_KEY else g.key,
            "top_percent": _top_percent(g.z, i),
            "strength": round(float(np.clip(g.z[i] / full, 0, 1)), 2),
            "matched": bool(g.z[i] >= g.need),
            "title_keyword": bool(g.title_hit[i]),
        })
    similar_liked = None
    if ctx.M is not None and ctx.liked and a.get("vec") is not None:
        sims = [(float(v @ a["vec"]), t) for t, v in ctx.liked]
        best = max(sims)
        if best[0] >= SIMILAR_LIKED and best[1] != a["title"]:
            similar_liked = best[1]
    notes = []
    if reason["kind"] == "explore":
        cat = reason.get("category")
        notes.append(f"평소 추천에 없던 {f'‘{cat}’ 분야의 ' if cat else ''}주요 뉴스를 골랐어요. 관심사 밖의 소식도 놓치지 않게 하려는 자리예요.")
    elif reason["kind"] == "latest":
        notes.append("관심사와 가까운 기사가 부족해 오늘의 주요 뉴스로 채웠어요." if ctx.groups
                     else "아직 관심사가 없어서 오늘 여러 언론사가 다룬 주요 뉴스를 보여 드려요.")
    return {
        "mode": ctx.mode,
        "bars": bars,
        "similar_liked": similar_liked,
        "coverage": a["_cov"] + 1,
        "notes": notes,
        "pool_size": len(ctx.pool),
    }


# ── 내 취향: 관심사를 모델이 어떻게 이해했는지 ─────────────

JOSA = ("으로", "에서", "까지", "부터", "에게", "처럼", "보다", "은", "는", "이", "가", "을", "를", "에", "의", "도", "와", "과", "로", "만")
STOP = {"단독", "속보", "종합", "영상", "포토", "사진", "기자", "뉴스", "오늘", "내일", "올해", "지난해", "이번", "관련",
        "위해", "대한", "통해", "있는", "없는", "했다", "한다", "이후", "가운데", "결국", "다시", "논란", "우려", "전망"}


def _tokens(title: str) -> set[str]:
    out = set()
    for w in re.findall(r"[가-힣A-Za-z0-9·]+", title):
        if re.search(r"\d", w):        # 숫자가 든 말(91주째, ks3 등)은 주제를 말해 주지 않음
            continue
        for j in JOSA:
            if w.endswith(j) and len(w) - len(j) >= 2:
                w = w[: -len(j)]
                break
        if len(w) >= 2 and w not in STOP:
            out.add(w.lower() if w.isascii() else w)
    return out


def _keywords(ctx: Context, members: np.ndarray, exclude: str, n: int = 5) -> list[str]:
    """묶음(members)의 제목에 유독 자주 나오는 단어 (전체 대비)."""
    if not members.any():
        return []
    docs = [_tokens(a["title"]) for a in ctx.pool]
    df_all = Counter(w for d in docs for w in d)
    sel = [docs[i] for i in np.where(members)[0]]
    df_sel = Counter(w for d in sel for w in d)
    ex = _norm(exclude)
    def same_as_interest(w: str) -> bool:   # 관심사 단어 자체는 빼고 '함께 나오는 말'만
        return bool(ex) and (_norm(w) in ex or ex in _norm(w))

    scored = [(c / len(sel) * math.log((len(docs) + 1) / (df_all[w] + 1)), w)
              for w, c in df_sel.items() if c >= 2 and not same_as_interest(w)]
    return [w for _, w in sorted(scored, reverse=True)[:n]]


def _map_points(ctx: Context, rec_idx: set[int]) -> dict:
    """오늘의 기사 지도: 관심사를 원 둘레에 두고, 기사는 관심사 점수만큼 그쪽으로 끌려감.
    위치는 추천에 쓴 관심사별 z점수로만 정해짐 (임의의 2D 투영이 아님)."""
    K = len(ctx.groups)
    angles = [-math.pi / 2 + 2 * math.pi * k / K for k in range(K)]
    anchors = [{"label": "좋아요 취향" if g.key == BEHAVIOR_KEY else g.key,
                "x": round(math.cos(t), 3), "y": round(math.sin(t), 3)} for g, t in zip(ctx.groups, angles)]
    Z = np.vstack([g.z for g in ctx.groups])                      # (관심사, 기사)
    W = np.clip(Z, 0, None) ** 2                                  # 가까운 관심사일수록 크게
    denom = W.sum(0) + MATCH_Z ** 2                               # 관련 기준(z=3)에 닿으면 중심과 관심사 사이 절반쯤
    ax = np.array([math.cos(t) for t in angles])
    ay = np.array([math.sin(t) for t in angles])
    xs, ys = (ax @ W) / denom, (ay @ W) / denom
    # 점이 너무 많으면 화면이 뭉개지므로: 추천 기사 + 관심사와 가까운 기사 + 나머지 일부
    near = set(np.argsort(-Z.max(0), kind="stable")[:160].tolist())
    rest = [i for i in range(len(ctx.pool)) if i not in near and i not in rec_idx][:: max(1, len(ctx.pool) // 120)]
    chosen = sorted(rec_idx | near | set(rest))
    points = []
    for i in chosen:
        a = ctx.pool[i]
        k = int(Z[:, i].argmax())
        points.append({"id": a["id"], "x": round(float(xs[i]), 3), "y": round(float(ys[i]), 3),
                       "title": a["title"], "source": a["source"], "link": a["link"],
                       "recommended": i in rec_idx,
                       "nearest": anchors[k]["label"] if Z[k, i] >= ctx.groups[k].need else None})
    return {"anchors": anchors, "points": points}


def taste(ctx: Context, rec_idx: set[int]) -> dict:
    interests = []
    for g in ctx.groups:
        members = g.z >= g.need
        interests.append({
            "label": "좋아요 취향" if g.key == BEHAVIOR_KEY else g.key,
            "behavior": g.key == BEHAVIOR_KEY,
            "matched_today": int(members.sum()),
            "keywords": _keywords(ctx, members, "" if g.key == BEHAVIOR_KEY else g.key),
        })
    blocked = [{"label": text, "count": int(hit.sum()),
                "examples": [ctx.pool[i]["title"] for i in np.where(hit)[0][:2]]} for text, hit in ctx.blocked_by]
    return {
        "mode": ctx.mode,
        "pool_size": len(ctx.pool),
        "likes_count": len(ctx.liked),
        "min_likes_for_behavior": MIN_LIKES_FOR_BEHAVIOR,
        "interests": interests,
        "blocked": blocked,
        "map": _map_points(ctx, rec_idx) if ctx.groups else None,
    }


def clean(a: dict) -> dict:
    for k in ("_age_h", "_bigrams", "_cov", "vec"):
        a.pop(k, None)
    return a
