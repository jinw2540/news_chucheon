// 브라우저 안의 데이터 소스 (PLAN.md 7장)
// 예전 로컬 서버(/api/...)와 같은 모양으로 답해서 화면 코드(app.js)는 그대로 쓴다.
// - 기사·벡터: GitHub Actions가 1시간마다 만든 data/ 파일
// - 추천·설명·내 취향: recommender.js가 브라우저에서 계산
// - 관심사·행동 기록: 이 기기(localStorage)에 먼저 저장 → Netlify 함수로 보관 (실패하면 모아 뒀다가 다시 보냄)
// - 직접 입력한 관심사의 벡터: /api/interest-vectors (Hugging Face API), 도착 전에는 키워드 방식
// - 날씨: Open-Meteo 직접
(function (root) {
  "use strict";
  const R = root.Recommender;
  const LOCAL_KEY = "briefing-local-v1";
  const PER_SOURCE_IN_TAB = 5;
  const CITY = { name: "서울", lat: 37.5665, lon: 126.978 };
  const WEATHER = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "안개",
    51: "이슬비", 53: "이슬비", 55: "이슬비", 56: "어는 이슬비", 57: "어는 이슬비",
    61: "비", 63: "비", 65: "강한 비", 66: "어는 비", 67: "어는 비",
    71: "눈", 73: "눈", 75: "많은 눈", 77: "싸락눈",
    80: "소나기", 81: "소나기", 82: "강한 소나기", 85: "눈 소나기", 86: "눈 소나기",
    95: "뇌우", 96: "뇌우", 99: "뇌우",
  };
  const STATE_EVENTS = new Set(["click", "like", "unlike", "dislike", "undislike", "hide", "unhide"]);

  // ── 이 기기에 저장하는 것 ─────────────────────────────
  function newId() {
    return root.crypto?.randomUUID ? root.crypto.randomUUID() : `u-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  }
  const local = (() => {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(LOCAL_KEY)) || {}; } catch { /* 비공개 창 등 */ }
    return {
      uid: saved.uid || newId(),
      profile: saved.profile || null,           // {onboarded, interests, dislikes, updated_at}
      articleState: saved.articleState || {},   // 기사 ID → {liked, disliked, hidden, read}
      recent: saved.recent || [],                // 최근에 반응한 기사 ID (앞쪽이 최근)
      vectors: saved.vectors || {},              // 직접 입력한 관심사 → 벡터
      outbox: saved.outbox || [],                // 아직 서버로 못 보낸 행동 기록
    };
  })();
  function persist() {
    try {
      // 오래된 상태는 정리 (최근 2,000개 기사만)
      local.recent = local.recent.slice(0, 2000);
      const keep = new Set(local.recent);
      for (const k of Object.keys(local.articleState)) if (!keep.has(k)) delete local.articleState[k];
      localStorage.setItem(LOCAL_KEY, JSON.stringify(local));
    } catch { /* 저장소를 쓸 수 없어도 화면은 동작 */ }
  }
  persist();

  // ── 서버(Netlify 함수) ────────────────────────────────
  async function remote(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: { "x-user-id": local.uid, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error(`${res.status} ${path}`);
    return res.json();
  }

  // ── 데이터 파일 ───────────────────────────────────────
  let data = null;
  async function load() {
    if (data) return data;
    const get = (name) => fetch(`data/${name}`, { cache: "no-cache" }).then((r) => {
      if (!r.ok) throw new Error(`${r.status} data/${name}`);
      return name.endsWith(".bin") ? r.arrayBuffer() : r.json();
    });
    const [meta, articles, vecBuf, suggested] = await Promise.all(
      [get("meta.json"), get("articles.json"), get("vectors.bin"), get("suggested.json")]);
    const vectors = R.unpackVectors(vecBuf);
    data = {
      meta,
      articles: articles.articles,
      vectors,
      byId: new Map(articles.articles.map((a, i) => [a.id, i])),
      suggested: new Map(suggested.items.map((it) => [it.text, Float64Array.from(it.vector)])),
    };
    return data;
  }

  // ── 관심사 벡터 ───────────────────────────────────────
  const listeners = [];
  const vecFor = (t) => data.suggested.get(t) || (local.vectors[t] ? Float64Array.from(local.vectors[t]) : null);
  let fetching = null;
  /** 벡터가 없는 직접 입력 관심사를 API로 받아 옴. 새로 받은 게 있으면 화면에 알림 */
  function ensureVectors() {
    const p = local.profile;
    if (!p || !data || fetching) return;
    const need = [...p.interests, ...p.dislikes].filter((t) => !data.suggested.has(t) && !local.vectors[t]);
    if (!need.length) return;
    fetching = remote("POST", "/api/interest-vectors", { texts: need })
      .then((res) => {
        const got = Object.keys(res.vectors || {});
        for (const t of got) local.vectors[t] = res.vectors[t];
        persist();
        if (got.length) listeners.forEach((fn) => fn());
      })
      .catch(() => { /* 키워드 방식으로 계속, 다음에 다시 시도 */ })
      .finally(() => { fetching = null; });
  }

  // ── 추천 계산 ─────────────────────────────────────────
  function context() {
    const st = local.articleState;
    const pool = [];
    data.articles.forEach((a, i) => {
      const s = st[a.id] || {};
      if (s.hidden || s.disliked) return;
      pool.push({ ...a, vec: data.vectors[i], liked: !!s.liked, read: !!s.read });
    });
    const p = local.profile || { interests: [], dislikes: [] };
    const known = (k) => data.byId.has(k);
    const liked = local.recent.filter((k) => st[k]?.liked && known(k)).slice(0, 50)
      .map((k) => [data.articles[data.byId.get(k)].title, data.vectors[data.byId.get(k)]]);
    const disliked = local.recent.filter((k) => st[k]?.disliked && known(k)).slice(0, 50)
      .map((k) => data.vectors[data.byId.get(k)]);
    return R.buildContext(pool, p.interests.map((t) => [t, vecFor(t)]), p.dislikes.map((t) => [t, vecFor(t)]), liked, disliked);
  }

  function publicArticle(a) {
    const s = local.articleState[a.id] || {};
    const summary = a.ai_summary || a.rss_summary || null;
    return {
      id: a.id, source: a.source, title: a.title, link: a.link, published_at: a.published_at, has_time: a.has_time,
      categories: a.categories, summary, summary_kind: a.ai_summary ? "ai" : a.rss_summary ? "rss" : null,
      summary_status: a.summary_status, liked: !!s.liked, read: !!s.read,
    };
  }

  // ── 화면이 쓰는 함수들 (예전 /api/... 와 같은 모양) ───────
  const api = {
    onChange(fn) { listeners.push(fn); },

    async meta() {
      const d = await load();
      return { sources: d.meta.sources, categories: d.meta.categories, updated_at: d.meta.updated_at,
        feeds_failed: d.meta.feeds_failed, suggested_interests: d.meta.suggested_interests };
    },

    async weather() {
      const q = new URLSearchParams({
        latitude: CITY.lat, longitude: CITY.lon, current: "temperature_2m,weather_code",
        daily: "temperature_2m_max,temperature_2m_min,precipitation_probability_max", timezone: "Asia/Seoul", forecast_days: 1,
      });
      const d = await fetch(`https://api.open-meteo.com/v1/forecast?${q}`).then((r) => r.json());
      return { city: CITY.name, temp: Math.round(d.current.temperature_2m), desc: WEATHER[d.current.weather_code] || "날씨 정보",
        hi: Math.round(d.daily.temperature_2m_max[0]), lo: Math.round(d.daily.temperature_2m_min[0]),
        pop: d.daily.precipitation_probability_max[0] };
    },

    async profile() {
      await load();
      // 서버에 더 최근 관심사가 있으면 그것을 씀 (다른 기기에서 바꾼 경우 등)
      try {
        const server = await remote("GET", "/api/profile");
        const mine = local.profile;
        if (server && (!mine || (server.updated_at || "") > (mine.updated_at || ""))) local.profile = server;
      } catch { /* 서버에 닿지 않으면 이 기기의 관심사로 */ }
      local.profile ??= { onboarded: false, interests: [], dislikes: [] };
      persist();
      ensureVectors();
      const { onboarded, interests, dislikes } = local.profile;
      return { onboarded, interests, dislikes };
    },

    async saveProfile(p) {
      local.profile = { onboarded: p.onboarded !== false, interests: p.interests, dislikes: p.dislikes, updated_at: new Date().toISOString() };
      persist();
      remote("PUT", "/api/profile", local.profile).catch(() => { /* 다음에 다시 저장됨 */ });
      ensureVectors();   // 직접 입력한 관심사: 우선 키워드로 추천, 벡터가 오면 다시 계산
      const { onboarded, interests, dislikes } = local.profile;
      return { onboarded, interests, dislikes };
    },

    async recommendations() {
      await load();
      const ctx = context();
      const picks = R.recommend(ctx);
      return { mode: ctx.mode, items: picks.map((p) => ({ ...publicArticle(p.article), reason: p.reason, explain: R.explain(ctx, p) })) };
    },

    async articles(category, sources) {
      const d = await load();
      const wanted = sources ? new Set(sources) : null;
      const st = local.articleState;
      const rows = d.articles.filter((a) => !(st[a.id]?.hidden || st[a.id]?.disliked)
        && (category === "전체" || a.categories.includes(category)) && (!wanted || wanted.has(a.source)));
      // 언론사당 최신 5건, 언론사를 번갈아 섞기 (기사 파일은 이미 최신순)
      const groups = new Map();
      for (const a of rows) {
        const g = groups.get(a.source) || [];
        if (g.length < PER_SOURCE_IN_TAB) g.push(a);
        groups.set(a.source, g);
      }
      const out = [];
      for (let i = 0; i < PER_SOURCE_IN_TAB; i++) for (const g of groups.values()) if (i < g.length) out.push(g[i]);
      return { items: out.map(publicArticle) };
    },

    async events(events) {
      for (const e of events) {
        if (!STATE_EVENTS.has(e.type)) continue;
        const s = (local.articleState[e.article_id] ||= {});
        if (e.type === "click") s.read = true;
        else if (e.type === "like") { s.liked = true; s.disliked = false; }
        else if (e.type === "unlike") s.liked = false;
        else if (e.type === "dislike") { s.disliked = true; s.liked = false; }
        else if (e.type === "undislike") s.disliked = false;
        else if (e.type === "hide") s.hidden = true;
        else if (e.type === "unhide") s.hidden = false;
        local.recent = [e.article_id, ...local.recent.filter((k) => k !== e.article_id)];
      }
      local.outbox.push(...events);
      local.outbox = local.outbox.slice(-500);
      persist();
      const batch = local.outbox.slice(0, 100);
      try {
        await remote("POST", "/api/events", { events: batch });
        local.outbox = local.outbox.slice(batch.length);
        persist();
      } catch { /* 이 기기에는 반영됨. 서버로는 다음에 다시 보냄 */ }
      return { ok: true };
    },

    async deleteMe() {
      await remote("DELETE", "/api/profile");     // 서버 기록부터 지움 (실패하면 화면이 알려 줌)
      try { localStorage.removeItem(LOCAL_KEY); } catch { /* 무시 */ }
      return { ok: true };
    },

    async taste() {
      await load();
      const ctx = context();
      return R.taste(ctx, new Set(R.recommend(ctx).map((p) => p.idx)));
    },
  };

  root.BriefingData = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
