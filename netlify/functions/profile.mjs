// 관심사 조회·저장·전체 삭제 → Netlify Blobs `profiles` (키: 사용자 ID)
// DELETE는 '내 기록 전체 삭제': 관심사와 행동 기록(events)을 모두 지움
import { getStore } from "@netlify/blobs";
import { cleanList, json, userId } from "../lib/common.mjs";

const EMPTY = { onboarded: false, interests: [], dislikes: [] };

export default async (req) => {
  const uid = userId(req);
  if (!uid) return json({ error: "사용자 ID가 없거나 형식이 맞지 않아요" }, 400);
  const profiles = getStore("profiles");

  if (req.method === "GET") {
    return json((await profiles.get(uid, { type: "json" })) ?? EMPTY);
  }
  if (req.method === "PUT") {
    let body;
    try { body = await req.json(); } catch { return json({ error: "JSON 형식이 아니에요" }, 400); }
    const profile = {
      onboarded: body.onboarded !== false,
      interests: cleanList(body.interests),
      dislikes: cleanList(body.dislikes),
      updated_at: new Date().toISOString(),
    };
    await profiles.setJSON(uid, profile);
    return json(profile);
  }
  if (req.method === "DELETE") {
    await profiles.delete(uid);
    const events = getStore("events");
    const { blobs } = await events.list({ prefix: `${uid}/` });
    await Promise.all(blobs.map((b) => events.delete(b.key)));
    return json({ ok: true, deleted_event_days: blobs.length });
  }
  return json({ error: "지원하지 않는 요청이에요" }, 405);
};

export const config = { path: "/api/profile" };
