"""15개 언론사 RSS를 수집·정리해서 DB에 저장한다.

피드별로 확인한 문제와 처리 (PLAN.md 6장 '피드별로 정리해야 할 점'):
- 연합뉴스: 깨진 문자 → 읽기 전에 제어 문자와 잘못된 & 정리
- 한겨레: 제목에 HTML 태그, 기사별 시각 없음 → 태그 제거, 처음 수집한 시각 사용
- 머니투데이: 특수문자 두 번 인코딩, [포토] 기사 → 두 번 풀기, 제외
- 날짜 형식 제각각 / 미래 시각 → 한국 시간으로 통일, 미래면 수집 시각 사용
"""
import html
import json
import os
import logging
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import httpx

from . import db

log = logging.getLogger("collector")

KST = timezone(timedelta(hours=9))
FEEDS_FILE = Path(__file__).resolve().parent / "feeds.json"
USER_AGENT = "MorningBriefing/0.1 (personal news reader; RSS only)"
REQUEST_GAP_SECONDS = 1.0     # 같은 언론사 서버에 연달아 요청할 때 간격
KEEP_DAYS = 30                # 기사 보관 기간
DC_DATE = "{http://purl.org/dc/elements/1.1/}date"
HANGUL = re.compile(r"[가-힣]")
TRACKING_PARAMS = {"plink", "cooper"}   # SBS가 같은 기사에 붙였다 뗐다 하는 추적용 값
PHOTO_TITLE = re.compile(r"^\s*\[\s*(포토|사진|화보|영상)\s*\]")   # 사진·영상만 있는 기사는 제외
UNKNOWN_AGE = timedelta(hours=6)  # 첫 수집 때 시각을 모르는 기사는 이만큼 지난 것으로 추정
# 수집 간격(시간). 로컬 서버는 1시간마다, GitHub Actions는 하루 한 번(24) → 간격이 길면 '처음 본 시각'이
# 실제 발행 시각과 크게 달라지므로, 시각 없는 기사는 매번 시각을 숨기고 추정값으로 정렬
COLLECT_INTERVAL_HOURS = float(os.getenv("COLLECT_INTERVAL_HOURS", "1"))
# 요약문 앞의 기자 머리말: "[서울=뉴시스] 홍길동 기자 =", "(서울=연합뉴스) 홍길동 기자 =" 등
BYLINE = re.compile(
    r"^\s*(?:[\[\(【][^\]\)】=]{1,20}=[^\]\)】]{1,20}[\]\)】]\s*)?"
    r"(?:[가-힣]{2,4}(?:\s*·\s*[가-힣]{2,4})*\s*(?:기자|특파원|기상캐스터)\s*=\s*)?"
)


def strip_byline(text: str) -> str:
    return BYLINE.sub("", text, count=1).strip()


def load_feeds() -> dict:
    return json.loads(FEEDS_FILE.read_text(encoding="utf-8"))


# ── 정리 도우미 ──────────────────────────────────────────

def sanitize_xml(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = value
    for _ in range(2):  # 두 번 인코딩된 특수문자(&amp;#039;)까지 풀기
        text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_link(link: str) -> str:
    """같은 기사가 여러 피드에 있어도 한 번만 저장되도록 추적용 파라미터 제거."""
    parts = urlsplit(link.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def parse_date(value: str | None, now: datetime) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    dt = None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    dt = dt.astimezone(KST)
    if dt > now + timedelta(minutes=10):  # 미래 시각은 믿지 않음
        return None
    return dt


# ── 피드 하나 처리 ───────────────────────────────────────

def parse_feed(raw: bytes, now: datetime) -> list[dict]:
    root = ET.fromstring(sanitize_xml(raw))
    items = []
    for item in root.iter("item"):
        title = clean_text(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        if not title or not link or PHOTO_TITLE.match(title):
            continue
        if len(HANGUL.findall(title)) < 2:   # 조선일보 피드에 섞인 일본어판 등 외국어 기사 제외
            continue
        summary = strip_byline(clean_text(item.findtext("description")))
        published = parse_date(item.findtext("pubDate") or item.findtext(DC_DATE), now)
        items.append({
            "title": title,
            "link": normalize_link(html.unescape(link)),
            "summary": summary[:500] if len(summary) >= 20 else None,
            "published": published,
        })
    return items


def save_items(conn, feed: dict, items: list[dict], now: datetime, first_time: bool) -> int:
    new = 0
    now_iso = now.isoformat(timespec="seconds")
    for it in items:
        if it["published"]:
            published, has_time = it["published"].isoformat(timespec="seconds"), 1
        else:
            # 시각이 없는 피드(한겨레 등): 처음 본 시각을 사용 (서버가 계속 수집하므로 실제와 비슷).
            # 단, 피드를 처음 수집할 때는 예전 기사까지 섞여 있으므로
            # 정렬용으로 6시간 전으로 추정하고 화면에서는 시각을 숨김
            if first_time or COLLECT_INTERVAL_HOURS > 2:
                published, has_time = (now - UNKNOWN_AGE).isoformat(timespec="seconds"), 0
            else:
                published, has_time = now_iso, 1
        cur = conn.execute(
            """INSERT INTO articles (link, source, title, rss_summary, published_at, has_time, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(link) DO NOTHING""",
            (it["link"], feed["source"], it["title"], it["summary"], published, has_time, now_iso),
        )
        new += cur.rowcount
        if feed["category"] != "전체":
            conn.execute(
                """INSERT OR IGNORE INTO article_categories (article_id, category)
                   SELECT id, ? FROM articles WHERE link = ?""",
                (feed["category"], it["link"]),
            )
    return new


def fetch_host_feeds(client: httpx.Client, feeds: list[dict]) -> list[tuple[dict, bytes | None, str | None]]:
    """같은 언론사 서버의 피드는 간격을 두고 차례로 요청."""
    results = []
    for i, feed in enumerate(feeds):
        if i:
            time.sleep(REQUEST_GAP_SECONDS)
        try:
            res = client.get(feed["url"])
            res.raise_for_status()
            results.append((feed, res.content, None))
        except Exception as e:  # 한 피드가 실패해도 나머지는 계속
            results.append((feed, None, f"{type(e).__name__}: {e}"[:300]))
    return results


def collect() -> dict:
    """모든 피드를 수집. 결과 요약을 돌려준다."""
    started = time.time()
    feeds = load_feeds()["feeds"]
    by_host: dict[str, list[dict]] = defaultdict(list)
    for f in feeds:
        by_host[urlsplit(f["url"]).netloc].append(f)

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=8) as pool:
            fetched = [r for group in pool.map(lambda fs: fetch_host_feeds(client, fs), by_host.values()) for r in group]

    now = datetime.now(KST)
    now_iso = now.isoformat(timespec="seconds")
    ok = failed = new_total = 0
    with db.session() as conn:
        for feed, raw, error in fetched:
            prev = conn.execute("SELECT last_success_at FROM feed_status WHERE url = ?", (feed["url"],)).fetchone()
            first_time = not (prev and prev["last_success_at"])
            if raw is not None:
                try:
                    items = parse_feed(raw, now)
                    new_total += save_items(conn, feed, items, now, first_time)
                    ok += 1
                    conn.execute(
                        """INSERT INTO feed_status (url, source, category, last_success_at, last_error, last_count)
                           VALUES (?, ?, ?, ?, NULL, ?)
                           ON CONFLICT(url) DO UPDATE SET last_success_at = excluded.last_success_at,
                               last_error = NULL, last_count = excluded.last_count""",
                        (feed["url"], feed["source"], feed["category"], now_iso, len(items)),
                    )
                    continue
                except ET.ParseError as e:
                    error = f"ParseError: {e}"
            failed += 1
            log.warning("피드 실패 %s %s: %s", feed["source"], feed["category"], error)
            conn.execute(
                """INSERT INTO feed_status (url, source, category, last_error)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET last_error = excluded.last_error""",
                (feed["url"], feed["source"], feed["category"], error),
            )

        cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
        removed = conn.execute("DELETE FROM articles WHERE published_at < ?", (cutoff,)).rowcount
        db.set_meta(conn, "last_collect_at", now_iso)

    summary = {"feeds_ok": ok, "feeds_failed": failed, "new_articles": new_total,
               "removed_articles": removed, "seconds": round(time.time() - started, 1)}
    log.info("수집 완료 %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init()
    print(collect())
