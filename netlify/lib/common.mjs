// Netlify 함수들이 함께 쓰는 도우미 (함수 폴더 밖에 두어 함수로 배포되지 않게 함)

// 로그인 전에는 브라우저가 만든 무작위 ID로 사용자를 구분 (PLAN.md 7-5)
const USER_ID = /^[A-Za-z0-9-]{16,64}$/;

export function userId(req) {
  const id = req.headers.get("x-user-id") || "";
  return USER_ID.test(id) ? id : null;
}

export function json(data, status = 200) {
  return Response.json(data, { status, headers: { "Cache-Control": "no-store" } });
}

/** 관심사 목록 정리: 앞뒤 공백 제거, 30자로 자르기, 중복·빈 값 제거, 최대 개수 */
export function cleanList(xs, max = 30) {
  if (!Array.isArray(xs)) return [];
  return [...new Set(xs.map((x) => String(x).trim().slice(0, 30)).filter(Boolean))].slice(0, max);
}

export async function sha1(text) {
  const digest = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function env(name) {
  return globalThis.Netlify?.env?.get(name) ?? process.env[name];
}

/** 한국 시간 기준 날짜 (행동 기록을 날짜별로 나눠 저장) */
export function kstDate(d = new Date()) {
  return new Date(d.getTime() + 9 * 3600 * 1000).toISOString().slice(0, 10);
}
