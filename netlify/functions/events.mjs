// 행동 기록(노출·클릭·👍·👎·숨김) 저장 → Netlify Blobs `events` (키: 사용자ID/날짜)
// 화면은 같은 기록을 브라우저에도 저장해 추천에 바로 반영하고, 여기 기록은 파이프라인의 요약 후보 계산과
// 6단계 신경망 학습에 쓰임
import { getStore } from "@netlify/blobs";
import { json, kstDate, userId } from "../lib/common.mjs";

const TYPES = new Set(["impression", "click", "like", "unlike", "dislike", "undislike", "hide", "unhide"]);
const ARTICLE_ID = /^[0-9a-f]{12}$/;
const MAX_PER_REQUEST = 100;
const MAX_PER_DAY = 3000;

export default async (req) => {
  if (req.method !== "POST") return json({ error: "지원하지 않는 요청이에요" }, 405);
  const uid = userId(req);
  if (!uid) return json({ error: "사용자 ID가 없거나 형식이 맞지 않아요" }, 400);
  let body;
  try { body = await req.json(); } catch { return json({ error: "JSON 형식이 아니에요" }, 400); }
  const at = new Date().toISOString();
  const incoming = (Array.isArray(body.events) ? body.events : []).slice(0, MAX_PER_REQUEST)
    .filter((e) => e && ARTICLE_ID.test(e.article_id) && TYPES.has(e.type))
    .map((e) => ({ article_id: e.article_id, type: e.type, at }));
  if (!incoming.length) return json({ ok: true, saved: 0 });

  const store = getStore("events");
  const key = `${uid}/${kstDate()}`;
  const today = (await store.get(key, { type: "json" })) ?? [];
  const merged = today.concat(incoming).slice(-MAX_PER_DAY);
  await store.setJSON(key, merged);
  return json({ ok: true, saved: incoming.length });
};

export const config = { path: "/api/events" };
