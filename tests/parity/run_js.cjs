// tests/parity/run.py가 부르는 JavaScript 쪽 실행기.
// 입력(표준입력): 상황 목록 JSON / 인자: 데이터 폴더, 기준 시각(ms)
const fs = require("fs");
const path = require("path");
const R = require(path.join(__dirname, "../../static/recommender.js"));

const [dataDir, nowMs] = process.argv.slice(2);
const articles = JSON.parse(fs.readFileSync(path.join(dataDir, "articles.json"), "utf8")).articles;
const buf = fs.readFileSync(path.join(dataDir, "vectors.bin"));
const vectors = R.unpackVectors(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const now = Number(nowMs);

const asVec = (v) => (v ? Float64Array.from(v) : null);
const results = cases.map((sc) => {
  const gone = new Set([...sc.hidden, ...sc.disliked]);
  const byId = new Map(articles.map((a, i) => [a.id, [a, vectors[i]]]));
  const pool = articles.map((a, i) => ({ ...a, vec: vectors[i], liked: sc.liked.includes(a.id), read: false }))
    .filter((a) => !gone.has(a.id));
  const ctx = R.buildContext(pool,
    sc.likes.map(([t, v]) => [t, asVec(v)]), sc.dislikes.map(([t, v]) => [t, asVec(v)]),
    sc.liked.map((k) => [byId.get(k)[0].title, byId.get(k)[1]]), sc.disliked.map((k) => byId.get(k)[1]), now);
  const picks = R.recommend(ctx);
  return {
    mode: ctx.mode,
    picks: picks.map((p) => ({ id: p.article.id, reason: p.reason, explain: R.explain(ctx, p) })),
    taste: R.taste(ctx, new Set(picks.map((p) => p.idx))),
  };
});
process.stdout.write(JSON.stringify(results));
