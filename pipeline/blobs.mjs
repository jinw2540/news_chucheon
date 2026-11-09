// GitHub Actions에서 Netlify Blobs의 사용자 데이터를 파일로 내보냄 → pipeline/build.py --users
// 필요한 환경 변수: NETLIFY_SITE_ID, NETLIFY_AUTH_TOKEN (Blobs 접근 권한이 있는 개인 토큰)
// 실행: node pipeline/blobs.mjs export users.json
import { writeFileSync } from "node:fs";
import { getStore } from "@netlify/blobs";

const EVENT_DAYS = 30;   // 최근 30일 행동 기록만

function store(name) {
  const siteID = process.env.NETLIFY_SITE_ID;
  const token = process.env.NETLIFY_AUTH_TOKEN;
  if (!siteID || !token) throw new Error("NETLIFY_SITE_ID와 NETLIFY_AUTH_TOKEN이 필요해요");
  return getStore({ name, siteID, token });
}

async function exportUsers(outPath) {
  const users = {};
  const profiles = store("profiles");
  for (const { key } of (await profiles.list()).blobs) {
    users[key] = { profile: await profiles.get(key, { type: "json" }), events: [] };
  }

  const since = new Date(Date.now() - EVENT_DAYS * 86400000).toISOString().slice(0, 10);
  const events = store("events");
  for (const { key } of (await events.list()).blobs) {
    const [uid, day] = key.split("/");
    if (!day || day < since) continue;
    users[uid] ??= { profile: null, events: [] };
    users[uid].events.push(...((await events.get(key, { type: "json" })) ?? []));
  }

  const interestVectors = {};
  const vectors = store("interest-vectors");
  for (const { key } of (await vectors.list()).blobs) {
    const item = await vectors.get(key, { type: "json" });
    if (item?.text && item.vector) interestVectors[item.text] = item.vector;
  }

  writeFileSync(outPath, JSON.stringify({ users, interest_vectors: interestVectors }));
  const n = Object.values(users);
  console.log(`사용자 ${n.length}명, 행동 기록 ${n.reduce((s, u) => s + u.events.length, 0)}건, ` +
    `관심사 벡터 ${Object.keys(interestVectors).length}개 → ${outPath}`);
}

const [cmd, out] = process.argv.slice(2);
if (cmd === "export" && out) await exportUsers(out);
else { console.error("사용법: node pipeline/blobs.mjs export users.json"); process.exit(1); }
