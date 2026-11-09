// 아침 브리핑 화면 — datasource.js가 주는 데이터(기사 파일 + 브라우저 추천 계산)로 그린다.
(() => {
  "use strict";

  // ── 아이콘 (Lucide 스타일 선 아이콘) ──────────────────
  const svg = (inner) =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
  const ICONS = {
    up: svg('<path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/>'),
    down: svg('<path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/>'),
    x: svg('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'),
    sliders: svg('<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>'),
    filter: svg('<path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"/>'),
    check: svg('<path d="M20 6 9 17l-5-5"/>'),
  };

  // ── 데이터 ───────────────────────────────────────────
  // datasource.js: 데이터 파일 + 브라우저 추천 계산 + Netlify 함수 (예전 /api/... 와 같은 모양으로 답함)
  const api = window.BriefingData;

  // ── 화면 설정 (이 기기에만 저장: 마지막 탭, 언론사 필터) ──
  const UI_KEY = "briefing-ui-v1";
  const ui = (() => {
    try { return JSON.parse(localStorage.getItem(UI_KEY)) || {}; } catch { return {}; }
  })();
  function saveUi() {
    try {
      localStorage.setItem(UI_KEY, JSON.stringify({ tab: state.tab, sources: state.sources ? [...state.sources] : null }));
    } catch { /* 저장소를 쓸 수 없어도 화면은 동작 */ }
  }

  // ── 상태 ──────────────────────────────────────────────
  const state = {
    meta: null,
    profile: { onboarded: true, interests: [], dislikes: [] },
    recs: [],                    // [{id, reason}]
    list: [],                    // [id]
    byId: new Map(),             // id → 기사
    gone: new Set(),             // 이번 화면에서 숨김·별로 처리한 기사
    tab: ui.tab || "전체",
    sources: Array.isArray(ui.sources) ? new Set(ui.sources) : null, // null = 전체
  };

  // ── 작은 도우미 ───────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  function el(tag, props = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "html") n.innerHTML = v; // 위의 고정 아이콘에만 사용
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const c of kids.flat()) if (c != null && c !== false) n.append(c.nodeType ? c : document.createTextNode(c));
    return n;
  }
  function timeAgo(iso) {
    const m = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000));
    if (m < 1) return "방금";
    if (m < 60) return `${m}분 전`;
    if (m < 60 * 24) return `${Math.floor(m / 60)}시간 전`;
    return `${Math.floor(m / 1440)}일 전`;
  }
  const splitList = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
  const remember = (items) => items.forEach((a) => state.byId.set(a.id, { ...state.byId.get(a.id), ...a }));
  const visibleRecs = () => state.recs.filter((r) => !state.gone.has(r.id));

  function send(events) {
    api.events(events).catch(() => {});   // 이 기기에는 바로 반영, 서버로는 실패하면 다음에 다시 보냄
  }

  // ── 헤더 ──────────────────────────────────────────────
  function renderHeader(weather) {
    const now = new Date();
    $("date").textContent = now.toLocaleDateString("ko-KR", { year: "numeric", month: "long", day: "numeric", weekday: "long" });
    const h = now.getHours();
    $("greeting").textContent = h < 5 ? "늦은 밤이에요" : h < 11 ? "좋은 아침이에요" : h < 17 ? "좋은 오후예요" : "좋은 저녁이에요";
    const chip = $("weather");
    if (weather) {
      chip.hidden = false;
      chip.textContent = `${weather.city} ${weather.desc} ${weather.temp}°`;
      chip.setAttribute("aria-label", `${weather.city} ${weather.temp}도 ${weather.desc}, 자세히 보기`);
      chip.onclick = () => toast(`${weather.city} ${weather.desc} · 최고 ${weather.hi}° / 최저 ${weather.lo}° · 강수확률 ${weather.pop}%`);
    } else {
      chip.hidden = true;
    }
    $("open-settings").innerHTML = ICONS.sliders;
    $("filter-icon").innerHTML = ICONS.filter;
  }

  // ── 오늘의 브리핑 ─────────────────────────────────────
  function summaryNode(a) {
    if (a.summary_status === "pending") return el("p", { class: "summary pending" }, "AI 요약 준비 중");
    if (!a.summary) return null;
    const p = el("p", { class: "summary", title: "눌러서 펼치기" },
      a.summary_kind === "ai" && el("span", { class: "ai-label" }, "AI 요약"),
      a.summary);
    p.addEventListener("click", () => p.classList.toggle("expanded"));
    return p;
  }

  function metaLine(a) {
    return el("div", { class: "meta" },
      el("span", { class: "badge" }, a.source),
      a.has_time && el("span", {}, timeAgo(a.published_at)),
      a.read && el("span", { class: "read-mark" }, "읽음"));
  }

  function titleLink(a, cls) {
    return el("a", {
      class: cls, href: a.link, target: "_blank", rel: "noopener",
      onclick: () => markRead(a.id),
    }, a.title);
  }

  // "왜 이 기사?" — 추천할 때 실제로 계산한 관심사별 점수를 막대로
  function whyPanel(a) {
    const e = a.explain;
    const panel = el("div", { class: "why", id: `why-${a.id}`, hidden: true });
    if (!e) return panel;
    if (e.bars.length) {
      panel.append(
        el("p", { class: "why-head" }, "내 관심사와 얼마나 가까운가"),
        el("ul", { class: "why-bars" }, ...e.bars.map((b) =>
          el("li", { class: b.matched ? "on" : null },
            el("span", { class: "why-label", title: b.label }, b.label),
            el("span", { class: "why-track", "aria-hidden": "true" }, el("span", { class: "why-fill", style: `width:${Math.round(b.strength * 100)}%` })),
            el("span", { class: "why-rank" }, `상위 ${b.top_percent}%`)))));
    }
    const checks = [
      ...e.bars.filter((b) => b.title_keyword).map((b) => `제목에 ‘${b.label}’ 단어가 들어 있어요`),
      e.similar_liked && `좋아요한 ‘${e.similar_liked}’ 기사와 비슷해요`,
      e.coverage >= 2 && `${e.coverage}개 언론사가 함께 다룬 뉴스예요`,
      ...e.notes,
    ].filter(Boolean);
    if (checks.length) panel.append(el("ul", { class: "why-checks" }, ...checks.map((c) => el("li", {}, c))));
    if (e.bars.length) panel.append(el("p", { class: "why-foot" }, `순위는 오늘 기사 ${e.pool_size.toLocaleString()}건과 비교한 거예요.`));
    return panel;
  }

  function card(rec, lead) {
    const a = state.byId.get(rec.id);
    const panel = whyPanel(a);
    const reason = el("button", {
      class: `reason ${rec.reason.kind}`, type: "button", "aria-expanded": "false", "aria-controls": panel.id,
      title: "왜 이 기사를 추천했는지 보기",
      onclick: (e) => {
        const open = panel.hidden;
        panel.hidden = !open;
        e.currentTarget.setAttribute("aria-expanded", String(open));
      },
    }, rec.reason.label, el("span", { class: "chev", "aria-hidden": "true" }, "▾"));
    return el("li", { class: "card" + (lead ? " lead" : "") + (a.read ? " is-read" : ""), "data-id": a.id },
      metaLine(a),
      titleLink(a, "card-title"),
      summaryNode(a),
      el("div", { class: "card-foot" },
        reason,
        el("div", { class: "actions" },
          el("button", { class: "icon-btn like", type: "button", "aria-label": "좋아요", "aria-pressed": String(a.liked), html: ICONS.up, onclick: (e) => toggleLike(a.id, e.currentTarget) }),
          el("button", { class: "icon-btn", type: "button", "aria-label": "별로예요", html: ICONS.down, onclick: () => dismiss(a.id, "dislike") }),
          el("button", { class: "icon-btn", type: "button", "aria-label": "숨기기", html: ICONS.x, onclick: () => dismiss(a.id, "hide") }))),
      panel);
  }

  function renderBriefing() {
    const note = $("briefing-note");
    const { interests } = state.profile;
    if (interests.length) {
      note.replaceChildren(
        el("span", {}, `관심사 ${interests.slice(0, 4).join(" · ")}${interests.length > 4 ? ` 외 ${interests.length - 4}개` : ""} 기준`),
        el("button", { class: "link-btn", type: "button", onclick: () => openSheet("sheet-settings") }, "바꾸기"),
        el("button", { class: "link-btn", type: "button", onclick: () => openSheet("sheet-taste") }, "내 취향 보기"));
    } else {
      note.replaceChildren(
        el("span", {}, "관심사를 알려주시면 맞춰 드릴게요."),
        el("button", { class: "link-btn", type: "button", onclick: () => openSheet("sheet-onboarding") }, "관심사 고르기"));
    }
    const recs = visibleRecs();
    if (!recs.length) {
      $("cards").replaceChildren(el("li", { class: "empty" }, "지금은 추천할 기사가 없어요. 잠시 뒤 다시 확인해 주세요."));
    } else {
      $("cards").replaceChildren(...recs.map((r, i) => card(r, i === 0))); // 첫 기사는 1면 톱처럼 크게
    }
    updateProgress();
  }

  function updateProgress() {
    const recs = visibleRecs();
    const done = recs.filter((r) => state.byId.get(r.id)?.read).length;
    const t = $("progress-text");
    const all = recs.length > 0 && done === recs.length;
    t.textContent = all ? "브리핑 완료" : `${done} / ${recs.length} 읽음`;
    t.classList.toggle("done", all);
    $("progress-fill").style.width = recs.length ? `${(done / recs.length) * 100}%` : "0";
  }

  async function loadRecs() {
    const data = await api.recommendations();
    remember(data.items);
    state.recs = data.items.map((a) => ({ id: a.id, reason: a.reason }));
    renderBriefing();
    // 노출 기록: 나중에 "보여줬지만 누르지 않은 기사"를 학습에 씀
    send(state.recs.map((r) => ({ article_id: r.id, type: "impression" })));
  }


  // ── 행동 ──────────────────────────────────────────────
  function markRead(id) {
    const a = state.byId.get(id);
    if (!a) return;
    send([{ article_id: id, type: "click" }]);
    if (a.read) return;
    a.read = true;
    const c = document.querySelector(`.card[data-id="${id}"]`);
    if (c) {
      c.classList.add("is-read");
      c.querySelector(".meta").append(el("span", { class: "read-mark" }, "읽음"));
    }
    document.querySelectorAll(`.row[data-id="${id}"]`).forEach((r) => r.classList.add("is-read"));
    updateProgress();
  }

  function toggleLike(id, btn) {
    const a = state.byId.get(id);
    a.liked = !a.liked;
    btn.setAttribute("aria-pressed", String(a.liked));
    send([{ article_id: id, type: a.liked ? "like" : "unlike" }]);
    if (a.liked) toast("비슷한 기사를 더 보여드릴게요");
  }

  function dismiss(id, type) {
    state.gone.add(id);
    send([{ article_id: id, type }]);
    const node = document.querySelector(`.card[data-id="${id}"]`);
    if (node) {
      node.classList.add("collapsing");
      setTimeout(() => { node.hidden = true; }, 200);
    }
    updateProgress();
    renderList();
    toast(type === "dislike" ? "비슷한 기사를 덜 보여드릴게요" : "기사를 숨겼어요", {
      action: "되돌리기",
      onAction: () => {
        state.gone.delete(id);
        send([{ article_id: id, type: type === "dislike" ? "undislike" : "unhide" }]);
        if (node) { node.hidden = false; requestAnimationFrame(() => node.classList.remove("collapsing")); }
        updateProgress();
        renderList();
      },
    });
  }

  // ── 분야별 뉴스 ───────────────────────────────────────
  function renderTabs() {
    const cats = state.meta.categories;
    if (!cats.includes(state.tab)) state.tab = "전체";
    $("tabs").replaceChildren(...cats.map((c) =>
      el("button", {
        class: "tab", type: "button", role: "tab", "aria-selected": String(c === state.tab),
        onclick: () => { state.tab = c; saveUi(); renderTabs(); loadList(); },
      }, c)));
    const total = state.meta.sources.length;
    const n = state.sources ? state.sources.size : total;
    const btn = $("open-filter");
    btn.classList.toggle("active", n < total);
    $("filter-label").textContent = n < total ? `필터 ${n}` : "필터";
    btn.setAttribute("aria-label", n < total ? `언론사 필터, ${n}곳 선택됨` : "언론사 필터");
  }

  function renderList() {
    const items = state.list.map((id) => state.byId.get(id)).filter((a) => a && !state.gone.has(a.id));
    if (!items.length) {
      $("list").replaceChildren(el("li", { class: "empty" },
        el("span", {}, state.sources && state.sources.size === 0 ? "선택한 언론사가 없어요." : "선택한 언론사의 기사가 없어요."),
        el("button", { class: "btn-ghost", type: "button", onclick: () => openSheet("sheet-filter") }, "필터 열기")));
      return;
    }
    $("list").replaceChildren(...items.map((a) =>
      el("li", {},
        el("a", {
          class: "row" + (a.read ? " is-read" : ""), href: a.link, target: "_blank", rel: "noopener", "data-id": a.id,
          onclick: () => markRead(a.id),
        },
          el("span", { class: "row-title" }, a.title),
          el("span", { class: "meta" }, el("span", { class: "badge" }, a.source), a.has_time && el("span", {}, timeAgo(a.published_at)))))));
  }

  let listToken = 0;
  async function loadList() {
    const token = ++listToken;
    if (state.sources && state.sources.size === 0) {
      state.list = [];
      renderList();
      return;
    }
    try {
      const data = await api.articles(state.tab, state.sources ? [...state.sources] : null);
      if (token !== listToken) return; // 탭을 빠르게 바꾼 경우 이전 응답은 버림
      remember(data.items);
      state.list = data.items.map((a) => a.id);
      renderList();
    } catch {
      if (token === listToken) $("list").replaceChildren(el("li", { class: "empty" }, "기사를 불러오지 못했어요."));
    }
  }

  // ── 바닥글 ────────────────────────────────────────────
  function renderFoot() {
    const { updated_at: updated, feeds_failed: failed } = state.meta;
    const lines = [];
    if (updated) {
      const t = new Date(updated).toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "numeric", minute: "2-digit" });
      const stale = Date.now() - Date.parse(updated) > 14 * 3600 * 1000;   // 가장 긴 간격(19시→07시 12시간) + 여유
      lines.push(el("p", { class: stale ? "stale" : null },
        `뉴스 업데이트 ${t} · 매일 오전 7시·오후 1시·오후 7시에 새로 모아요`, stale && " · 업데이트가 늦어지고 있어요"));
    } else {
      lines.push(el("p", { class: "stale" }, "아직 뉴스를 수집하지 않았어요. 잠시 뒤 새로고침해 주세요."));
    }
    if (failed) lines.push(el("p", {}, `일부 피드(${failed}개)를 불러오지 못했어요.`));
    lines.push(
      el("p", {}, "출처: 각 언론사 RSS · 날씨: Open-Meteo"),
      el("p", {}, "AI 요약은 틀릴 수 있어요. 정확한 내용은 원문에서 확인해 주세요."),
      el("p", {}, "관심사와 읽은 기록은 추천을 위해 이 기기와 서버(Netlify)에 저장돼요. 설정에서 언제든 지울 수 있어요."));
    $("foot").replaceChildren(...lines);
  }

  // ── 시트 (아래에서 올라오는 창) ───────────────────────
  let current = null;
  let lastFocus = null;
  function openSheet(id) {
    closeSheet(false);
    const s = $(id);
    if (id === "sheet-onboarding") buildPicker("ob");
    if (id === "sheet-settings") { buildPicker("st"); $("st-dislikes").value = state.profile.dislikes.join(", "); }
    if (id === "sheet-filter") buildSourceFilter();
    if (id === "sheet-taste") loadTaste();
    lastFocus = document.activeElement;
    s.inert = false;
    s.classList.add("open");
    $("backdrop").classList.add("open");
    document.body.classList.add("no-scroll");
    current = s;
    // 칩에 포커스를 주면 선택된 것처럼 보여서, 시트 제목에 포커스를 둠
    const heading = s.querySelector("h2");
    heading.tabIndex = -1;
    setTimeout(() => heading.focus(), 60);
  }
  function closeSheet(restoreFocus = true) {
    if (!current) return;
    current.classList.remove("open");
    current.inert = true;
    $("backdrop").classList.remove("open");
    document.body.classList.remove("no-scroll");
    current = null;
    if (restoreFocus) lastFocus?.focus?.();
  }

  // 관심사 고르기 (첫 방문 / 설정 공용)
  const draft = { ob: new Set(), st: new Set() };
  function buildPicker(prefix) {
    const set = (draft[prefix] = new Set(state.profile.interests));
    const box = $(`${prefix}-chips`);
    const draw = () => {
      const all = [...new Set([...state.meta.suggested_interests, ...set])];
      box.replaceChildren(...all.map((name) => {
        const on = set.has(name);
        return el("button", {
          class: "chip", type: "button", "aria-pressed": String(on), html: on ? ICONS.check : null,
          onclick: () => { on ? set.delete(name) : set.add(name); draw(); },
        }, name);
      }));
    };
    const input = $(`${prefix}-custom`);
    const add = () => { splitList(input.value).forEach((v) => set.add(v)); input.value = ""; draw(); };
    $(`${prefix}-add`).onclick = add;
    input.onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); add(); } };
    input.value = "";
    draw();
  }
  function takeDraft(prefix) {
    splitList($(`${prefix}-custom`).value).forEach((v) => draft[prefix].add(v));
    return [...draft[prefix]];
  }

  async function saveProfile(changes, message) {
    try {
      state.profile = await api.saveProfile({ ...state.profile, ...changes });
    } catch {
      toast("저장하지 못했어요. 서버 연결을 확인해 주세요.");
      return;
    }
    closeSheet();
    state.gone.clear();
    try { await loadRecs(); } catch { /* 아래 알림으로 충분 */ }
    if (message) toast(message);
  }

  function buildSourceFilter() {
    const all = state.meta.sources;
    $("ft-sources").replaceChildren(...all.map((s) => {
      const input = el("input", { type: "checkbox", checked: !state.sources || state.sources.has(s) });
      input.addEventListener("change", () => {
        const set = state.sources ? new Set(state.sources) : new Set(all);
        input.checked ? set.add(s) : set.delete(s);
        state.sources = set.size === all.length ? null : set;
        saveUi(); renderTabs(); loadList();
      });
      return el("label", {}, input, s);
    }));
  }

  async function resetAll() {
    if (!confirm("관심사와 좋아요·숨김·읽음 기록을 모두 지울까요? 되돌릴 수 없어요.")) return;
    try {
      await api.deleteMe();
      location.reload();
    } catch {
      toast("지우지 못했어요. 서버 연결을 확인해 주세요.");
    }
  }

  // ── 내 취향 ───────────────────────────────────────────
  async function loadTaste() {
    const body = $("tt-body");
    body.replaceChildren(el("p", { class: "sheet-desc" }, "계산하는 중…"));
    try {
      renderTaste(await api.taste());
    } catch {
      body.replaceChildren(el("p", { class: "sheet-desc" }, "불러오지 못했어요. 서버 연결을 확인해 주세요."));
    }
  }

  function renderTaste(t) {
    const body = $("tt-body");
    if (!t.interests.length) {
      body.replaceChildren(el("div", { class: "empty" },
        el("span", {}, `관심사를 고르거나 좋아요를 ${t.min_likes_for_behavior}개 이상 누르면 내 취향이 보여요.`),
        el("button", { class: "btn-ghost", type: "button", onclick: () => openSheet("sheet-onboarding") }, "관심사 고르기")));
      return;
    }
    const sections = [];

    // 1) 오늘의 기사 지도
    if (t.map) {
      const pick = el("div", { class: "taste-pick" }, el("span", { class: "muted" }, "점을 누르면 어떤 기사인지 보여요."));
      sections.push(el("section", { class: "taste-section" },
        el("h3", {}, "오늘의 기사 지도"),
        el("p", { class: "taste-note" }, "내 관심사를 둘레에 두고, 오늘 기사를 관심사와 가까운 만큼 그쪽으로 끌어당겨 놓았어요. 가운데는 어느 관심사와도 멀어요."),
        mapSvg(t.map, pick),
        el("div", { class: "taste-legend" },
          el("span", {}, el("i", { class: "lg rec" }), "추천된 기사"),
          el("span", {}, el("i", { class: "lg" }), "오늘의 다른 기사")),
        pick));
    }

    // 2) AI가 이해한 내 관심사
    const rows = t.interests.map((i) =>
      el("div", { class: "taste-row" },
        el("div", {}, el("span", { class: "name" }, i.label), el("span", { class: "count" }, `오늘 ${i.matched_today}건`)),
        i.keywords.length
          ? el("div", { class: "kw" }, ...i.keywords.map((k) => el("span", {}, k)))
          : el("p", { class: "taste-note" }, "오늘은 함께 자주 나온 단어가 없어요.")));
    if (!t.interests.some((i) => i.behavior)) {
      rows.push(el("div", { class: "taste-row" },
        el("div", {}, el("span", { class: "name" }, "좋아요 취향"), el("span", { class: "count" }, `좋아요 ${t.likes_count} / ${t.min_likes_for_behavior}개`)),
        el("p", { class: "taste-note" }, `👍를 ${t.min_likes_for_behavior}개 이상 누르면 좋아요한 기사들로 취향을 배워요.`)));
    }
    sections.push(el("section", { class: "taste-section" },
      el("h3", {}, "AI가 이해한 내 관심사"),
      el("p", { class: "taste-note" }, "관심사마다 오늘 관련 있다고 본 기사들의 제목에 유독 자주 나온 단어예요. 생각과 다르면 관심사 글을 바꿔 보세요."),
      ...rows));

    // 3) 관심 없는 것이 막은 기사
    if (t.blocked.length) {
      sections.push(el("section", { class: "taste-section" },
        el("h3", {}, "관심 없는 것"),
        ...t.blocked.map((b) => el("div", { class: "taste-row" },
          el("div", {}, el("span", { class: "name" }, b.label), el("span", { class: "count" }, `오늘 ${b.count}건을 걸렀어요`)),
          b.examples.length ? el("p", { class: "taste-note" }, `예: ${b.examples.join(" / ")}`) : null)),
        el("p", { class: "taste-note" }, "‘정치’처럼 넓은 말보다 ‘특검’처럼 구체적인 말이 잘 걸러져요.")));
    }

    body.replaceChildren(...sections, el("p", { class: "taste-note" },
      t.mode === "keyword" ? "지금은 AI 모델 없이 글자 일치로 추천하고 있어요." : `오늘 기사 ${t.pool_size.toLocaleString()}건과 비교해 계산했어요.`));
  }

  // 관심사를 둘레에 두고 기사를 점으로 찍는 지도 (서버가 추천 점수로 계산한 위치)
  function mapSvg(map, pick) {
    const NS = "http://www.w3.org/2000/svg";
    const s = (tag, attrs = {}, ...kids) => {
      const n = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
      kids.forEach((k) => n.append(k));
      return n;
    };
    const root = s("svg", { viewBox: "-1.45 -1.3 2.9 2.6", class: "taste-map", role: "img", "aria-label": "오늘의 기사 지도" });
    root.append(s("circle", { cx: 0, cy: 0, r: 1, class: "map-ring" }));
    for (const a of map.anchors) {
      root.append(s("line", { x1: 0, y1: 0, x2: a.x, y2: a.y, class: "map-spoke" }));
      root.append(s("rect", { x: a.x - 0.03, y: a.y - 0.03, width: 0.06, height: 0.06, class: "map-anchor" }));
      const anchor = Math.abs(a.x) < 0.2 ? "middle" : a.x > 0 ? "start" : "end";
      root.append(s("text", {
        x: a.x * 1.1 + (anchor === "start" ? 0.02 : anchor === "end" ? -0.02 : 0), y: a.y * 1.1 + (a.y > 0.2 ? 0.07 : a.y < -0.2 ? -0.03 : 0.025),
        "text-anchor": anchor, class: "map-label",
      }, a.label));
    }
    let selected = null;
    const choose = (p, dot) => {
      selected?.classList.remove("sel");
      dot.classList.add("sel");
      selected = dot;
      pick.replaceChildren(
        el("a", { href: p.link, target: "_blank", rel: "noopener", onclick: () => send([{ article_id: p.id, type: "click" }]) }, p.title),
        el("span", { class: "meta" }, el("span", { class: "badge" }, p.source),
          el("span", {}, p.recommended ? "추천된 기사" : "추천되지 않은 기사"),
          el("span", {}, p.nearest ? `‘${p.nearest}’ 쪽` : "어느 관심사와도 멀어요")));
    };
    // 다른 기사 먼저, 추천 기사는 위에 그림
    for (const p of [...map.points].sort((x, y) => x.recommended - y.recommended)) {
      const dot = s("circle", { cx: p.x, cy: p.y, r: p.recommended ? 0.032 : 0.016, class: "map-dot" + (p.recommended ? " rec" : "") },
        s("title", {}, p.title));
      if (p.recommended) { dot.setAttribute("tabindex", "0"); dot.setAttribute("role", "button"); dot.setAttribute("aria-label", p.title); }
      dot.addEventListener("click", () => choose(p, dot));
      dot.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(p, dot); } });
      root.append(dot);
    }
    return root;
  }

  // ── 알림 ──────────────────────────────────────────────
  let toastTimer;
  function toast(msg, opts = {}) {
    const t = $("toast");
    t.replaceChildren(el("span", {}, msg));
    if (opts.action) {
      t.append(el("button", { class: "toast-action", type: "button", onclick: () => { opts.onAction(); hideToast(); } }, opts.action));
    }
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, opts.action ? 5000 : 2600);
  }
  function hideToast() { $("toast").classList.remove("show"); }

  // ── 불러오는 중 / 오류 ────────────────────────────────
  function renderSkeleton() {
    const line = (w, h = 14) => el("div", { class: "sk", style: `width:${w};height:${h}px` });
    $("cards").replaceChildren(...[0, 1, 2].map(() =>
      el("li", { class: "sk-card", "aria-hidden": "true" }, line("30%", 12), line("92%", 20), line("70%", 20), line("100%"), line("80%"))));
    $("list").replaceChildren(...[0, 1, 2, 3, 4].map(() =>
      el("li", { class: "sk-row", "aria-hidden": "true" }, line("88%", 16), line("35%", 12))));
    $("progress-text").textContent = "불러오는 중";
  }

  function renderOffline() {
    $("progress-text").textContent = "";
    $("cards").replaceChildren(el("li", { class: "empty" },
      el("span", {}, "서버에 연결할 수 없어요. 서버가 켜져 있는지 확인해 주세요."),
      el("button", { class: "btn-ghost", type: "button", onclick: () => location.reload() }, "다시 시도")));
    $("list").replaceChildren();
  }

  // ── 시작 ──────────────────────────────────────────────
  function wire() {
    $("open-settings").onclick = () => openSheet("sheet-settings");
    $("open-filter").onclick = () => openSheet("sheet-filter");
    $("backdrop").onclick = () => closeSheet();
    document.querySelectorAll("[data-close]").forEach((b) => {
      if (!b.textContent.trim()) b.innerHTML = ICONS.x;
      b.onclick = () => closeSheet();
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });

    $("ob-later").onclick = () => saveProfile({ onboarded: true });
    $("ob-start").onclick = () => {
      const interests = takeDraft("ob");
      saveProfile({ onboarded: true, interests }, interests.length ? "관심사에 맞춰 브리핑을 준비했어요" : null);
    };
    $("st-save").onclick = () => saveProfile(
      { interests: takeDraft("st"), dislikes: splitList($("st-dislikes").value) }, "관심사를 반영했어요");
    $("st-reset").onclick = resetAll;
    $("ft-all").onclick = () => { state.sources = null; saveUi(); buildSourceFilter(); renderTabs(); loadList(); };
    $("ft-none").onclick = () => { state.sources = new Set(); saveUi(); buildSourceFilter(); renderTabs(); loadList(); };
  }

  async function start() {
    renderHeader(null);
    wire();
    renderSkeleton();
    api.weather().then(renderHeader).catch(() => {});
    try {
      const [meta, profile] = await Promise.all([api.meta(), api.profile()]);
      state.meta = meta;
      state.profile = profile;
      if (state.sources) state.sources = new Set([...state.sources].filter((s) => meta.sources.includes(s)));
    } catch {
      renderOffline();
      return;
    }
    renderTabs();
    renderFoot();
    // 직접 입력한 관심사의 벡터가 도착하면 (처음엔 키워드 방식) 추천을 다시 계산
    api.onChange(() => loadRecs().catch(() => {}));
    await Promise.all([loadRecs().catch(renderOffline), loadList()]);
    if (!state.profile.onboarded) openSheet("sheet-onboarding");
  }

  start();
})();
