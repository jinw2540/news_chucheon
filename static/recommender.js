// 추천·설명·내 취향 — app/recommender.py를 JavaScript로 옮긴 것 (PLAN.md 7-7).
// 파이프라인(Python)이 같은 알고리즘으로 추천을 미리 계산해 그 기사만 AI 요약하므로,
// 두 버전은 같은 입력에 같은 결과를 내야 한다 → tests/parity 로 확인.
// 숫자는 모두 64비트 실수(Python 쪽도 float64로 맞춤), 정렬은 안정 정렬.
(function (root) {
  "use strict";

  const RECENT_HOURS = 48;
  const LIMIT = 10;
  const MAX_PER_SOURCE = 2;
  const EXPLORE_SLOTS = [3, 7];
  const SIMILAR_TITLE = 0.45;
  const SIMILAR_VECTOR = 0.90;
  const MAJOR_SOURCES = 3;
  const MATCH_Z = 3.0;
  const TITLE_KEYWORD_BONUS = 1.0;
  const DISLIKE_Z = 2.0;
  const MIN_LIKES_FOR_BEHAVIOR = 3;
  const FEEDBACK_MIX = 0.5;
  const MIN_ANCHORS = 2;
  const UNANCHORED_Z = 4.0;
  const SIMILAR_LIKED = 0.85;
  const BEHAVIOR_KEY = "좋아요 취향";
  const LATEST = { kind: "latest", label: "최신 주요 뉴스" };
  const EXPLORE = { kind: "explore", label: "새로운 주제 둘러보기" };
  const DIM = 384;

  // ── 작은 도우미 (Python과 같은 동작) ─────────────────
  const chars = (s) => Array.from(s);                       // 코드 포인트 단위 (Python 문자열과 같게)
  const norm = (s) => s.toLowerCase().split(/\s+/).join("");
  function bigrams(s) {
    const c = chars(norm(s));
    const out = new Set();
    for (let i = 0; i < c.length - 1; i++) out.add(c[i] + c[i + 1]);
    return out;
  }
  function dot(a, b) {
    let s = 0;
    for (let i = 0; i < a.length; i++) s += a[i] * b[i];
    return s;
  }
  function normalize(v) {
    let n = 0;
    for (let i = 0; i < v.length; i++) n += v[i] * v[i];
    n = Math.sqrt(n);
    const out = new Float64Array(v.length);
    for (let i = 0; i < v.length; i++) out[i] = v[i] / n;
    return out;
  }
  function zscore(x) {                                       // numpy 기본(모집단 표준편차)과 같게
    const n = x.length;
    let mean = 0;
    for (const v of x) mean += v;
    mean /= n;
    let ss = 0;
    for (const v of x) ss += (v - mean) * (v - mean);
    const std = Math.sqrt(ss / n);
    return x.map((v) => (std > 0 ? (v - mean) / std : 0));
  }
  const matVec = (M, v) => M.map((row) => dot(row, v));
  // 점수 큰 순, 같으면 앞에 있던 것 먼저 (numpy argsort(-x, kind="stable")와 같음)
  const argsortDesc = (x) => x.map((_, i) => i).sort((a, b) => x[b] - x[a] || a - b);
  const hoursBetween = (now, iso) => (now - Date.parse(iso)) / 3600000;

  // 벡터 파일: 기사마다 [배율 float32][int8 × 384] (pipeline/build.py pack_vectors)
  function unpackVectors(buffer) {
    const view = new DataView(buffer);
    const size = 4 + DIM;
    const n = Math.floor(buffer.byteLength / size);
    const out = new Array(n);
    for (let k = 0; k < n; k++) {
      const off = k * size;
      const scale = view.getFloat32(off, true);
      const v = new Float64Array(DIM);
      for (let i = 0; i < DIM; i++) v[i] = Math.fround(view.getInt8(off + 4 + i) * scale);  // Python: float32 곱셈
      out[k] = v;
    }
    return out;
  }

  // ── 점수 계산 ────────────────────────────────────────
  function anchors(text, docs) {
    const whole = norm(text);
    let hits = [];
    docs.forEach((d, i) => { if (d.includes(whole)) hits.push(i); });
    const words = text.split(/\s+/).filter((w) => w && chars(w).length >= 2).map(norm);
    if (hits.length < MIN_ANCHORS && words.length > 1) {
      hits = [];
      docs.forEach((d, i) => { if (words.every((w) => d.includes(w))) hits.push(i); });
    }
    return hits;
  }

  function expand(M, v, text, docs, zNeeded) {
    const hits = anchors(text, docs);
    if (hits.length < MIN_ANCHORS) return [Float64Array.from(v), Math.max(zNeeded, UNANCHORED_Z)];
    const mean = new Float64Array(v.length);
    for (const i of hits) for (let k = 0; k < v.length; k++) mean[k] += M[i][k];
    const u = new Float64Array(v.length);
    for (let k = 0; k < v.length; k++) u[k] = FEEDBACK_MIX * v[k] + (1 - FEEDBACK_MIX) * (mean[k] / hits.length);
    return [normalize(u), zNeeded];
  }

  /**
   * articles: [{id, source, title, link, published_at, has_time, categories, rss_summary, ai_summary, cov, vec, liked, read}]
   *   (숨김·별로예요 기사는 미리 빼고 넘김)
   * likes/dislikes: [[글, 벡터 또는 null]]   liked: [[제목, 벡터]] (최근 순)   dislikedVecs: [벡터]
   */
  function buildContext(articles, likes, dislikes, liked, dislikedVecs, now = Date.now()) {
    const pool = [];
    for (const a of articles) {
      const age = hoursBetween(now, a.published_at);
      if (age <= RECENT_HOURS) pool.push({ ...a, _age_h: age, _bigrams: bigrams(a.title), _cov: a.cov || 0 });
    }
    const vecReady = pool.length > 0 && pool.every((a) => a.vec);
    const M = vecReady ? pool.map((a) => a.vec) : null;
    const personal = likes.length > 0 || liked.length >= MIN_LIKES_FOR_BEHAVIOR;
    if (!personal || !pool.length) {
      return { pool, mode: "latest", groups: [], penalty: pool.map(() => 0), blockedBy: [], M, liked };
    }

    const ctx = { pool, mode: vecReady ? "vector" : "keyword", groups: [], penalty: pool.map(() => 0), blockedBy: [], M, liked };
    const titles = pool.map((a) => norm(a.title));
    const summaries = pool.map((a) => norm(a.rss_summary || ""));
    const docs = titles.map((t, i) => t + summaries[i]);
    const freshVec = pool.map((a) => -a._age_h / RECENT_HOURS);
    const freshKw = pool.map((a) => -a._age_h / 12);

    const keywordScores = (text) => {
      const k = norm(text);
      const titleHit = titles.map((t) => t.includes(k));
      const base = titleHit.map((h, i) => (h ? 2.0 : summaries[i].includes(k) ? 1.2 : 0.0));
      return [base, titleHit];
    };

    // 관심 없는 것
    for (const [text, v] of dislikes) {
      let hit;
      if (vecReady && v) {
        const [u, need] = expand(M, v, text, docs, DISLIKE_Z);
        const z = zscore(matVec(M, u));
        const k = norm(text);
        hit = z.map((zz, i) => zz >= need || titles[i].includes(k));
      } else {
        const k = norm(text);
        hit = docs.map((d) => d.includes(k));
      }
      ctx.blockedBy.push([text, hit]);
      ctx.penalty = ctx.penalty.map((p, i) => p - 3.0 * (hit[i] ? 1 : 0));
    }
    if (vecReady && dislikedVecs.length >= 2) {
      const u = new Float64Array(DIM);
      for (const v of dislikedVecs) for (let k = 0; k < DIM; k++) u[k] += v[k];
      for (let k = 0; k < DIM; k++) u[k] /= dislikedVecs.length;
      const z = zscore(matVec(M, normalize(u)));
      const hit = z.map((zz) => zz >= MATCH_Z);
      ctx.blockedBy.push(["별로예요 누른 기사", hit]);
      ctx.penalty = ctx.penalty.map((p, i) => p - 2.0 * (hit[i] ? 1 : 0));
    }

    // 관심사: 벡터가 있으면 벡터, 아직 없으면 키워드
    for (const [text, v] of likes) {
      const reason = { kind: "interest", label: `관심사 · ${text}` };
      if (vecReady && v) {
        const [u, need] = expand(M, v, text, docs, MATCH_Z);
        const k = norm(text);
        const titleHit = titles.map((t) => t.includes(k));
        const z = zscore(matVec(M, u)).map((zz, i) => zz + TITLE_KEYWORD_BONUS * (titleHit[i] ? 1 : 0));
        ctx.groups.push({ key: text, method: "vector", reason, z, need, titleHit,
          ok: z.map((zz) => zz >= need), score: z.map((zz, i) => zz + ctx.penalty[i] + freshVec[i]) });
      } else {
        const [base, titleHit] = keywordScores(text);
        ctx.groups.push({ key: text, method: "keyword", reason, z: base, need: 1.0, titleHit,
          ok: base.map((b) => b > 0), score: base.map((b, i) => b + ctx.penalty[i] + freshKw[i]) });
      }
    }
    if (vecReady && liked.length >= MIN_LIKES_FOR_BEHAVIOR) {
      const u = new Float64Array(DIM);
      liked.forEach(([, v], n) => { const w = Math.pow(0.9, n); for (let k = 0; k < DIM; k++) u[k] += v[k] * w; });
      const z = zscore(matVec(M, normalize(u)));
      ctx.groups.push({ key: BEHAVIOR_KEY, method: "vector", reason: { kind: "interest", label: "좋아요한 기사와 비슷해요" },
        z, need: MATCH_Z, titleHit: pool.map(() => false),
        ok: z.map((zz, i) => zz >= MATCH_Z && !pool[i].liked), score: z.map((zz, i) => zz + ctx.penalty[i] + freshVec[i]) });
    }
    return ctx;
  }

  // ── 기사 고르기 ──────────────────────────────────────
  function makePicker() {
    const picks = [];
    const perSource = {};
    return {
      picks,
      can(a, cap = MAX_PER_SOURCE) {
        if ((perSource[a.source] || 0) >= cap) return false;
        for (const p of picks) {
          const b = p.article;
          if (b.id === a.id) return false;
          let inter = 0;
          for (const g of a._bigrams) if (b._bigrams.has(g)) inter++;
          const union = a._bigrams.size + b._bigrams.size - inter;
          if (inter / Math.max(union, 1) >= SIMILAR_TITLE) return false;
          if (a.vec && b.vec && dot(a.vec, b.vec) >= SIMILAR_VECTOR) return false;
        }
        return true;
      },
      take(a, reason, idx, at = null) {
        const item = { article: a, reason, idx };
        picks.splice(at === null ? picks.length : Math.min(at, picks.length), 0, item);
        perSource[a.source] = (perSource[a.source] || 0) + 1;
      },
    };
  }

  const latestReason = (a) => (a._cov >= MAJOR_SOURCES
    ? { kind: "latest", label: `주요 뉴스 · ${a._cov + 1}개 언론사` } : LATEST);

  function recommend(ctx) {
    const pool = ctx.pool;
    const index = new Map(pool.map((a, i) => [a, i]));
    const latestKey = (a) => -Math.min(a._cov, 10) + a._age_h / 4;
    const latest = pool.map((a, i) => [a, i]).sort((x, y) => latestKey(x[0]) - latestKey(y[0]) || x[1] - y[1]).map((x) => x[0]);
    const picker = makePicker();

    if (ctx.mode === "latest") {
      for (const a of latest) {
        if (picker.picks.length < LIMIT && picker.can(a, 1)) picker.take(a, latestReason(a), index.get(a));
      }
      return picker.picks;
    }

    const personalSlots = LIMIT - EXPLORE_SLOTS.length;
    const queues = ctx.groups.map((g) => [g.reason, argsortDesc(g.score).filter((i) => g.ok[i])]);
    const pos = queues.map(() => 0);
    while (picker.picks.length < personalSlots) {
      let progressed = false;
      for (let g = 0; g < queues.length; g++) {
        const [reason, order] = queues[g];
        while (pos[g] < order.length) {
          const i = order[pos[g]];
          pos[g] += 1;
          if (picker.can(pool[i])) { picker.take(pool[i], reason, i); progressed = true; break; }
        }
        if (picker.picks.length >= personalSlots) break;
      }
      if (!progressed) break;
    }

    const blocked = new Set(pool.filter((_, i) => ctx.penalty[i] < 0).map((a) => a.id));
    for (const a of latest) {
      if (picker.picks.length >= personalSlots) break;
      if (!blocked.has(a.id) && picker.can(a)) picker.take(a, latestReason(a), index.get(a));
    }

    const used = new Set(picker.picks.flatMap((p) => p.article.categories));
    const candidates = [...latest.filter((a) => a.categories.length && !a.categories.some((c) => used.has(c))), ...latest];
    let s = 0;
    for (const a of candidates) {
      if (!blocked.has(a.id) && picker.can(a)) {
        const category = a.categories.find((c) => !used.has(c)) ?? null;
        picker.take(a, { ...EXPLORE, category }, index.get(a), EXPLORE_SLOTS[s]);
        s += 1;
        if (s >= EXPLORE_SLOTS.length) break;
      }
    }
    return picker.picks.slice(0, LIMIT);
  }

  // ── 설명: 왜 이 기사? ────────────────────────────────
  function topPercent(values, i) {
    let higher = 0;
    for (const v of values) if (v > values[i]) higher++;
    return Math.max(1, Math.ceil((100 * (higher + 1)) / values.length));
  }
  const round = (x, d) => Math.round(x * 10 ** d) / 10 ** d;

  function explain(ctx, pick) {
    const { idx: i, article: a, reason } = pick;
    const bars = ctx.groups.map((g) => {
      const full = g.method === "vector" ? g.need : 2.0;
      return {
        label: g.key === BEHAVIOR_KEY ? "좋아요 취향" : g.key,
        top_percent: topPercent(g.z, i),
        strength: round(Math.min(1, Math.max(0, g.z[i] / full)), 2),
        matched: g.z[i] >= g.need,
        title_keyword: !!g.titleHit[i],
      };
    });
    let similarLiked = null;
    if (ctx.M && ctx.liked.length && a.vec) {
      let best = null;
      for (const [t, v] of ctx.liked) {
        const d = dot(v, a.vec);
        if (!best || d > best[0] || (d === best[0] && t > best[1])) best = [d, t];
      }
      if (best[0] >= SIMILAR_LIKED && best[1] !== a.title) similarLiked = best[1];
    }
    const notes = [];
    if (reason.kind === "explore") {
      const cat = reason.category;
      notes.push(`평소 추천에 없던 ${cat ? `‘${cat}’ 분야의 ` : ""}주요 뉴스를 골랐어요. 관심사 밖의 소식도 놓치지 않게 하려는 자리예요.`);
    } else if (reason.kind === "latest") {
      notes.push(ctx.groups.length ? "관심사와 가까운 기사가 부족해 오늘의 주요 뉴스로 채웠어요."
        : "아직 관심사가 없어서 오늘 여러 언론사가 다룬 주요 뉴스를 보여 드려요.");
    }
    return { mode: ctx.mode, bars, similar_liked: similarLiked, coverage: a._cov + 1, notes, pool_size: ctx.pool.length };
  }

  // ── 내 취향 ──────────────────────────────────────────
  const JOSA = ["으로", "에서", "까지", "부터", "에게", "처럼", "보다", "은", "는", "이", "가", "을", "를", "에", "의", "도", "와", "과", "로", "만"];
  const STOP = new Set(["단독", "속보", "종합", "영상", "포토", "사진", "기자", "뉴스", "오늘", "내일", "올해", "지난해", "이번", "관련",
    "위해", "대한", "통해", "있는", "없는", "했다", "한다", "이후", "가운데", "결국", "다시", "논란", "우려", "전망"]);

  function tokens(title) {
    const out = new Set();
    for (let w of title.match(/[가-힣A-Za-z0-9·]+/g) || []) {
      if (/\d/.test(w)) continue;
      for (const j of JOSA) {
        if (w.endsWith(j) && chars(w).length - chars(j).length >= 2) { w = w.slice(0, w.length - j.length); break; }
      }
      if (chars(w).length >= 2 && !STOP.has(w)) out.add(/^[\x00-\x7F]*$/.test(w) ? w.toLowerCase() : w);
    }
    return out;
  }

  function keywords(ctx, members, exclude, n = 5) {
    if (!members.some(Boolean)) return [];
    const docs = ctx.pool.map((a) => tokens(a.title));
    const dfAll = new Map();
    for (const d of docs) for (const w of d) dfAll.set(w, (dfAll.get(w) || 0) + 1);
    const sel = docs.filter((_, i) => members[i]);
    const dfSel = new Map();
    for (const d of sel) for (const w of d) dfSel.set(w, (dfSel.get(w) || 0) + 1);
    const ex = norm(exclude);
    const same = (w) => !!ex && (ex.includes(norm(w)) || norm(w).includes(ex));
    const scored = [];
    for (const [w, c] of dfSel) {
      if (c >= 2 && !same(w)) scored.push([(c / sel.length) * Math.log((docs.length + 1) / (dfAll.get(w) + 1)), w]);
    }
    // Python sorted(reverse=True): 점수 큰 순, 같으면 단어도 큰 순
    scored.sort((x, y) => y[0] - x[0] || (y[1] > x[1] ? 1 : y[1] < x[1] ? -1 : 0));
    return scored.slice(0, n).map((x) => x[1]);
  }

  function mapPoints(ctx, recIdx) {
    const K = ctx.groups.length;
    const angles = ctx.groups.map((_, k) => -Math.PI / 2 + (2 * Math.PI * k) / K);
    const anchorsOut = ctx.groups.map((g, k) => ({
      label: g.key === BEHAVIOR_KEY ? "좋아요 취향" : g.key, x: round(Math.cos(angles[k]), 3), y: round(Math.sin(angles[k]), 3) }));
    const N = ctx.pool.length;
    const xs = new Float64Array(N);
    const ys = new Float64Array(N);
    const zmax = new Float64Array(N);
    const best = new Int32Array(N);
    for (let i = 0; i < N; i++) {
      let sw = 0, sx = 0, sy = 0, mx = -Infinity, bk = 0;
      for (let k = 0; k < K; k++) {
        const z = ctx.groups[k].z[i];
        const w = Math.max(z, 0) ** 2;
        sw += w; sx += Math.cos(angles[k]) * w; sy += Math.sin(angles[k]) * w;
        if (z > mx) { mx = z; bk = k; }
      }
      const denom = sw + MATCH_Z ** 2;
      xs[i] = sx / denom; ys[i] = sy / denom; zmax[i] = mx; best[i] = bk;
    }
    const near = new Set(argsortDesc(Array.from(zmax)).slice(0, 160));
    const step = Math.max(1, Math.floor(N / 120));
    const rest = [];
    for (let i = 0; i < N; i++) if (!near.has(i) && !recIdx.has(i)) rest.push(i);
    const chosen = [...new Set([...recIdx, ...near, ...rest.filter((_, j) => j % step === 0)])].sort((a, b) => a - b);
    const points = chosen.map((i) => {
      const a = ctx.pool[i];
      const k = best[i];
      return { id: a.id, x: round(xs[i], 3), y: round(ys[i], 3), title: a.title, source: a.source, link: a.link,
        recommended: recIdx.has(i), nearest: ctx.groups[k].z[i] >= ctx.groups[k].need ? anchorsOut[k].label : null };
    });
    return { anchors: anchorsOut, points };
  }

  function taste(ctx, recIdx) {
    const interests = ctx.groups.map((g) => {
      const members = g.z.map((z) => z >= g.need);
      return { label: g.key === BEHAVIOR_KEY ? "좋아요 취향" : g.key, behavior: g.key === BEHAVIOR_KEY,
        matched_today: members.filter(Boolean).length,
        keywords: keywords(ctx, members, g.key === BEHAVIOR_KEY ? "" : g.key) };
    });
    const blocked = ctx.blockedBy.map(([label, hit]) => ({
      label, count: hit.filter(Boolean).length,
      examples: ctx.pool.filter((_, i) => hit[i]).slice(0, 2).map((a) => a.title) }));
    return { mode: ctx.mode, pool_size: ctx.pool.length, likes_count: ctx.liked.length,
      min_likes_for_behavior: MIN_LIKES_FOR_BEHAVIOR, interests, blocked, map: ctx.groups.length ? mapPoints(ctx, recIdx) : null };
  }

  const api = { RECENT_HOURS, LIMIT, MIN_LIKES_FOR_BEHAVIOR, buildContext, recommend, explain, taste, unpackVectors, tokens, norm };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Recommender = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
