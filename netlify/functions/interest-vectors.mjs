// 직접 입력한 관심사 → 벡터 (PLAN.md 7-4)
// Hugging Face API(hf-inference)의 multilingual-e5-small로 만들고 Blobs `interest-vectors`에 저장.
// 같은 글은 다시 요청하지 않음. 파이프라인의 기사 벡터(32비트 모델)와 같은 벡터임을 확인함 (2026-11-09, 유사도 1.0)
import { getStore } from "@netlify/blobs";
import { cleanList, env, json, sha1 } from "../lib/common.mjs";

const HF_URL = "https://router.huggingface.co/hf-inference/models/intfloat/multilingual-e5-small/pipeline/feature-extraction";
const DIM = 384;

async function embed(texts) {
  const token = env("HF_TOKEN");
  if (!token) throw new Error("HF_TOKEN이 설정되지 않았어요");
  const res = await fetch(HF_URL, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ inputs: texts.map((t) => `query: ${t}`) }),   // e5 규칙: 관심사 글은 query:
    signal: AbortSignal.timeout(9000),
  });
  if (!res.ok) throw new Error(`Hugging Face API ${res.status}`);
  let out = await res.json();
  if (texts.length === 1 && typeof out[0] === "number") out = [out];        // 하나만 보냈을 때의 응답 모양
  return out.map((v) => {
    if (!Array.isArray(v) || v.length !== DIM) throw new Error("예상과 다른 응답 모양");
    const n = Math.hypot(...v);                                            // 이미 길이 1이지만 한 번 더 맞춤
    return v.map((x) => Math.round((x / n) * 1e6) / 1e6);
  });
}

export default async (req) => {
  if (req.method !== "POST") return json({ error: "지원하지 않는 요청이에요" }, 405);
  let body;
  try { body = await req.json(); } catch { return json({ error: "JSON 형식이 아니에요" }, 400); }
  const texts = cleanList(body.texts, 20);
  const store = getStore("interest-vectors");
  const keys = await Promise.all(texts.map((t) => sha1(`query:${t}`)));

  const vectors = {};
  const missing = [];
  await Promise.all(texts.map(async (t, i) => {
    const hit = await store.get(keys[i], { type: "json" });
    if (hit?.vector) vectors[t] = hit.vector;
    else missing.push(t);
  }));

  let error = null;
  if (missing.length) {
    try {
      const made = await embed(missing);
      await Promise.all(missing.map((t, i) => {
        vectors[t] = made[i];
        return store.setJSON(keys[texts.indexOf(t)], { text: t, vector: made[i], made_at: new Date().toISOString() });
      }));
    } catch (e) {
      error = String(e.message || e);   // 화면은 이 관심사를 키워드 방식으로 추천하고, 다음에 다시 요청
    }
  }
  return json({ vectors, missing: texts.filter((t) => !vectors[t]), error });
};

export const config = { path: "/api/interest-vectors" };
