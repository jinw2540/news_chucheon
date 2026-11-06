"""서버 시작점: DB 준비, 1시간마다 뉴스 수집·벡터 생성, API와 화면 제공.

실행: .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
처음 한 번 모델 내려받기: .venv/bin/python -m app.embedder download
AI 요약을 쓰려면 .env 파일에 OPENAI_API_KEY 설정 (.env.example 참고)
"""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv(dotenv_path=__import__("pathlib").Path(__file__).resolve().parent.parent / ".env")

from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from . import db, embedder, summarizer  # noqa: E402
from .api import router  # noqa: E402
from .collector import KST, collect  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "apscheduler", "openai", "trafilatura"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

COLLECT_EVERY_MINUTES = 60


def embed_pending() -> None:
    """벡터가 없는 기사·관심사를 벡터로 (모델이 없으면 건너뜀)."""
    embedder.embed_new_articles()
    with db.session() as conn:
        users = [r["id"] for r in conn.execute("SELECT id FROM users")]
    for uid in users:
        embedder.embed_interests(uid)


def collect_and_embed() -> None:
    collect()
    embed_pending()


def _first_run_time() -> datetime:
    """마지막 수집이 1시간 이상 지났으면 바로, 아니면 1시간이 차는 시점에 수집."""
    with db.session() as conn:
        last = db.get_meta(conn, "last_collect_at")
    now = datetime.now(KST)
    if not last:
        return now
    due = datetime.fromisoformat(last) + timedelta(minutes=COLLECT_EVERY_MINUTES)
    return max(now, due)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    summarizer.reset_stuck()
    if not summarizer.enabled():
        logging.getLogger("main").warning("OPENAI_API_KEY가 없어 AI 요약을 끕니다. 화면에는 RSS 요약문이 나옵니다.")
    scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    scheduler.add_job(collect_and_embed, "interval", minutes=COLLECT_EVERY_MINUTES, id="collect",
                      next_run_time=_first_run_time(), coalesce=True, max_instances=1)
    # 서버를 켤 때 밀린 벡터 작업이 있으면 바로 처리 (모델 로딩도 미리 해 둠)
    scheduler.add_job(embed_pending, id="embed_on_start", next_run_time=datetime.now(KST))
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="아침 브리핑", lifespan=lifespan)
app.include_router(router)
app.mount("/", StaticFiles(directory=db.ROOT / "static", html=True), name="static")
