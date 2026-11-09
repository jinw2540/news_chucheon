"""multilingual-e5-small(ONNX)로 기사·관심사를 384차원 벡터로 바꾼다.

PyTorch 없이 ONNX Runtime만 사용한다 (PLAN.md 4-2).
- EMBED_VARIANT=int8 (기본, 로컬 개발 서버): 8비트 압축 모델 118MB
- EMBED_VARIANT=fp32 (GitHub Actions 파이프라인): 원본 32비트 모델 470MB.
  Hugging Face API(hf-inference)와 **같은 벡터**를 냄 (2026-11-09 측정: 유사도 1.0) → API로 만든 관심사 벡터와 정확히 비교 가능
- 기사는 "passage: ", 관심사는 "query: "를 앞에 붙인다 (e5 모델 사용 규칙)
- 벡터는 길이 1로 정규화하므로 내적 = 코사인 유사도
- DB에는 float16(768바이트)으로 저장

모델 내려받기: .venv/bin/python -m app.embedder download
"""
import logging
import os
import threading
from pathlib import Path

import httpx
import numpy as np

from . import db

log = logging.getLogger("embedder")

MODEL_DIR = db.ROOT / "models" / "multilingual-e5-small"
HF_BASE = "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main"
VARIANT = os.getenv("EMBED_VARIANT", "int8")
MODEL_FILE = {"int8": "model.onnx", "fp32": "model_fp32.onnx"}[VARIANT]
FILES = {
    MODEL_FILE: {"int8": "onnx/model_quantized.onnx", "fp32": "onnx/model.onnx"}[VARIANT],
    "tokenizer.json": "tokenizer.json",
}
DIM = 384
BATCH = 32
MAX_TOKENS = {"passage": 192, "query": 32}
EMBED_RECENT_DAYS = 7     # 추천에 쓰는 최근 기사만 벡터로 만듦

_lock = threading.Lock()
_model = None


def download() -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, remote in FILES.items():
        dest = MODEL_DIR / name
        if dest.exists():
            continue
        tmp = dest.with_suffix(".part")
        log.info("내려받는 중: %s", remote)
        with httpx.stream("GET", f"{HF_BASE}/{remote}", follow_redirects=True, timeout=60) as res:
            res.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in res.iter_bytes(1 << 20):
                    f.write(chunk)
        tmp.rename(dest)


def is_ready() -> bool:
    return all((MODEL_DIR / n).exists() for n in FILES)


class _Model:
    def __init__(self):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2   # 작은 서버(1~2코어) 기준
        self.session = ort.InferenceSession(str(MODEL_DIR / MODEL_FILE), opts, providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.session.get_inputs()}
        self.tokenizer = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")

    def encode(self, texts: list[str], kind: str) -> np.ndarray:
        self.tokenizer.enable_truncation(MAX_TOKENS[kind])
        out = []
        for i in range(0, len(texts), BATCH):
            enc = self.tokenizer.encode_batch([f"{kind}: {t}" for t in texts[i:i + BATCH]])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.input_names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, feeds)[0]                     # (배치, 토큰, 384)
            m = mask[..., None].astype(np.float32)
            vec = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)   # 평균 풀링
            out.append(vec / np.linalg.norm(vec, axis=1, keepdims=True))
        return np.vstack(out).astype(np.float32) if out else np.zeros((0, DIM), np.float32)


def _get_model() -> "_Model | None":
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                if not is_ready():
                    log.warning("임베딩 모델이 없어 키워드 추천으로 대신합니다. 내려받기: python -m app.embedder download")
                    return None
                _model = _Model()
    return _model


def encode(texts: list[str], kind: str) -> np.ndarray | None:
    model = _get_model()
    return None if model is None else model.encode(texts, kind)


def to_blob(v: np.ndarray) -> bytes:
    return v.astype(np.float16).tobytes()


def from_blob(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float16).astype(np.float32)


def embed_new_articles() -> int:
    """벡터가 없는 최근 기사를 벡터로 바꿔 저장. 처리한 개수를 돌려준다."""
    if _get_model() is None:
        return 0
    from datetime import datetime, timedelta
    from .collector import KST

    since = (datetime.now(KST) - timedelta(days=EMBED_RECENT_DAYS)).isoformat(timespec="seconds")
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id, title, rss_summary FROM articles WHERE embedding IS NULL AND published_at >= ?", (since,)
        ).fetchall()
    done = 0
    for i in range(0, len(rows), 256):   # 조금씩 저장해서 중간에 꺼져도 다시 이어감
        chunk = rows[i:i + 256]
        vecs = encode([f"{r['title']}. {r['rss_summary'] or ''}".strip() for r in chunk], "passage")
        with db.session() as conn:
            conn.executemany("UPDATE articles SET embedding = ? WHERE id = ?",
                             [(to_blob(v), r["id"]) for v, r in zip(vecs, chunk)])
        done += len(chunk)
    if done:
        log.info("기사 벡터 생성 %d건", done)
    return done


def embed_interests(user_id: int) -> None:
    with db.session() as conn:
        rows = conn.execute("SELECT id, text FROM interests WHERE user_id = ? AND embedding IS NULL", (user_id,)).fetchall()
        if not rows:
            return
        vecs = encode([r["text"] for r in rows], "query")
        if vecs is None:
            return
        conn.executemany("UPDATE interests SET embedding = ? WHERE id = ?", [(to_blob(v), r["id"]) for v, r in zip(vecs, rows)])


if __name__ == "__main__":
    import sys
    import time

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.argv[1:] == ["download"]:
        download()
        print("완료:", MODEL_DIR)
    else:
        db.init()
        t = time.time()
        n = embed_new_articles()
        print(f"{n}건, {time.time() - t:.1f}초")
