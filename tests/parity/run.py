"""Python(app/recommender.py)과 JavaScript(static/recommender.js) 추천 일치 시험 (PLAN.md 7-7).

같은 데이터 파일(static/data)과 같은 기준 시각으로 여러 상황을 두 버전에 돌려,
추천 기사·이유·설명·내 취향이 같은지 비교한다. 한쪽을 고치면 반드시 함께 돌린다.

실행: .venv/bin/python -m tests.parity.run [데이터 폴더, 기본 static/data]
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from app import embedder, recommender
from pipeline.build import unpack_vectors

ROOT = Path(__file__).resolve().parents[2]
JS_RUNNER = Path(__file__).with_name("run_js.cjs")


def scenarios(articles: list[dict], suggested: dict) -> list[dict]:
    """여러 상황. 벡터는 JSON(64비트)으로 두 버전에 똑같이 넘김."""
    ids = [a["id"] for a in articles]
    pick = lambda words, n: [a["id"] for a in articles if any(w in a["title"] for w in words)][:n]
    custom = {t: [float(x) for x in v] for t, v in zip(["이더리움", "전세 대출"], embedder.encode(["이더리움", "전세 대출"], "query"))}
    vec = lambda t: suggested.get(t) or custom.get(t)
    return [
        {"name": "관심사 없음 (첫 화면)", "likes": [], "dislikes": [], "liked": [], "disliked": [], "hidden": []},
        {"name": "추천 목록 관심사 3개 + 관심 없음",
         "likes": [[t, vec(t)] for t in ["반도체", "전세 시장", "야구"]], "dislikes": [["특검", None]],
         "liked": [], "disliked": [], "hidden": []},
        {"name": "직접 입력 관심사 (벡터 있음)",
         "likes": [[t, vec(t)] for t in ["이더리움", "전세 대출", "AI"]], "dislikes": [],
         "liked": [], "disliked": [], "hidden": []},
        {"name": "벡터와 키워드가 섞임 (직접 입력 직후)",
         "likes": [["반도체", vec("반도체")], ["엔비디아", None]], "dislikes": [["코인", vec("코인")]],
         "liked": [], "disliked": [], "hidden": []},
        {"name": "좋아요 취향 + 별로예요 + 숨김",
         "likes": [["기후", vec("기후")]], "dislikes": [],
         "liked": pick(["우주", "위성", "로켓", "달 "], 4), "disliked": pick(["특검", "검찰"], 3), "hidden": ids[:5]},
    ]


def py_run(articles, vectors, sc, now):
    gone = set(sc["hidden"]) | set(sc["disliked"])
    by_id = {a["id"]: (a, v) for a, v in zip(articles, vectors)}
    pool = [{**a, "vec": v, "liked": a["id"] in sc["liked"], "read": False}
            for a, v in zip(articles, vectors) if a["id"] not in gone]
    as_vec = lambda v: None if v is None else np.asarray(v, np.float64)
    ctx = recommender.build_context(
        pool, [(t, as_vec(v)) for t, v in sc["likes"]], [(t, as_vec(v)) for t, v in sc["dislikes"]],
        [(by_id[k][0]["title"], by_id[k][1]) for k in sc["liked"]], [by_id[k][1] for k in sc["disliked"]], now=now)
    picks = recommender.recommend(ctx)
    return {
        "mode": ctx.mode,
        "picks": [{"id": p["article"]["id"], "reason": p["reason"], "explain": recommender.explain(ctx, p)} for p in picks],
        "taste": recommender.taste(ctx, {p["idx"] for p in picks}),
    }


def compare(py, js, path="") -> list[str]:
    """숫자 중 반올림한 값(strength, x, y)은 ±0.011까지 허용, 나머지는 정확히 같아야 함."""
    errs = []
    if isinstance(py, dict) and isinstance(js, dict):
        for k in sorted(set(py) | set(js)):
            if k not in py or k not in js:
                errs.append(f"{path}.{k}: 한쪽에만 있음")
            else:
                errs += compare(py[k], js[k], f"{path}.{k}")
    elif isinstance(py, list) and isinstance(js, list):
        if len(py) != len(js):
            errs.append(f"{path}: 개수 다름 {len(py)} vs {len(js)}")
        for i, (a, b) in enumerate(zip(py, js)):
            errs += compare(a, b, f"{path}[{i}]")
    elif isinstance(py, (int, float)) and isinstance(js, (int, float)) and not isinstance(py, bool):
        tol = 0.011 if path.split(".")[-1] in ("strength", "x", "y") else 0
        if abs(py - js) > tol:
            errs.append(f"{path}: {py} vs {js}")
    elif py != js:
        errs.append(f"{path}: {py!r} vs {js!r}")
    return errs


def main():
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "static" / "data"
    payload = json.loads((data_dir / "articles.json").read_text(encoding="utf-8"))
    articles = payload["articles"]
    vectors = unpack_vectors((data_dir / "vectors.bin").read_bytes())
    suggested = {it["text"]: it["vector"] for it in json.loads((data_dir / "suggested.json").read_text(encoding="utf-8"))["items"]}
    now = datetime.fromisoformat(payload["generated_at"])
    cases = scenarios(articles, suggested)

    py_results = [py_run(articles, vectors, sc, now) for sc in cases]
    js_out = subprocess.run(
        ["node", str(JS_RUNNER), str(data_dir), str(int(now.timestamp() * 1000))],
        input=json.dumps(cases), capture_output=True, text=True, check=True)
    js_results = json.loads(js_out.stdout)

    failed = 0
    for sc, py, js in zip(cases, py_results, js_results):
        errs = compare(py, js)
        ok = "통과" if not errs else f"불일치 {len(errs)}곳"
        print(f"[{ok}] {sc['name']}: 방식 {py['mode']}, 추천 {len(py['picks'])}개, 지도 점 "
              f"{len((py['taste']['map'] or {}).get('points', []))}개")
        for e in errs[:8]:
            print("    ", e)
        failed += bool(errs)
    print("결과:", "모두 일치" if not failed else f"{failed}개 상황 불일치")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
