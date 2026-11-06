"""추천된 기사만 원문 페이지에서 본문을 꺼낸다 (PLAN.md 4-8).

- 각 언론사 robots.txt에서 막은 주소는 가져오지 않음
- 같은 언론사에는 요청 사이에 간격을 둠, 기사당 한 번만 요청
- 본문은 요약에만 쓰고 저장하지 않음
- 1순위 trafilatura, 조선일보는 페이지 안 JSON, 동아일보는 본문 영역을 직접 읽음
"""
import html as html_lib
import json
import logging
import re
import threading
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura

from .collector import USER_AGENT, strip_byline

log = logging.getLogger("extractor")

HOST_GAP_SECONDS = 3.0
MAX_CHARS = 3000
MIN_CHARS = 200
ROBOTS_TTL = 24 * 3600

# 시험하면서 발견한 본문 속 잡음 (해당 줄을 지움)
NOISE = re.compile(
    r"(인공지능 기술로 자동 요약|흔들리는 나침반|Internet Explorer|무단\s*전재|재배포\s*금지|저작권자|Copyright|ⓒ|©"
    r"|[\w.+-]+@[\w-]+\.[\w.]+|기사\s*제보|구독하기|좋아요\s*\d|관련\s*기사|많이\s*본\s*뉴스)",
    re.IGNORECASE,
)

_robots: dict[str, tuple[float, RobotFileParser | None]] = {}
_last_hit: dict[str, float] = {}
_lock = threading.Lock()
_client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True)


def _robots_for(host: str, scheme: str) -> RobotFileParser | None:
    now = time.time()
    with _lock:
        cached = _robots.get(host)
        if cached and now - cached[0] < ROBOTS_TTL:
            return cached[1]
    parser = RobotFileParser()
    try:
        res = _client.get(f"{scheme}://{host}/robots.txt")
        if res.status_code >= 400:
            parser = None          # robots.txt가 없으면 제한 없음
        else:
            parser.parse(res.text.splitlines())
    except httpx.HTTPError:
        parser = None
    with _lock:
        _robots[host] = (now, parser)
    return parser


def allowed(link: str) -> bool:
    parts = urlsplit(link)
    parser = _robots_for(parts.netloc, parts.scheme)
    return parser is None or parser.can_fetch(USER_AGENT, link)


def _wait_turn(host: str) -> None:
    """같은 언론사 서버에는 HOST_GAP_SECONDS 간격으로만 요청."""
    while True:
        with _lock:
            now = time.time()
            ready = _last_hit.get(host, 0) + HOST_GAP_SECONDS
            if now >= ready:
                _last_hit[host] = now
                return
        time.sleep(ready - now)


def _chosun(html: str) -> str | None:
    m = re.search(r"Fusion\.globalContent\s*=\s*(\{.*?\});\s*Fusion\.", html, re.S)
    if not m:
        return None
    data = json.loads(m.group(1))
    paras = [html_lib.unescape(re.sub(r"<[^>]+>", "", c.get("content", "")))
             for c in data.get("content_elements", []) if c.get("type") == "text"]
    return "\n".join(p.strip() for p in paras if p.strip())


def _donga(html: str) -> str | None:
    """동아일보: trafilatura가 본문을 못 찾아 본문 영역(section.news_view)을 직접 읽음."""
    from lxml import html as lxml_html

    tree = lxml_html.fromstring(html)
    nodes = tree.xpath("//section[contains(concat(' ', normalize-space(@class), ' '), ' news_view ')]")
    if not nodes:
        return None
    node = nodes[0]
    for bad in node.xpath(".//script|.//style|.//figure|.//figcaption|.//*[contains(@class,'ad')]|.//*[contains(@class,'reporter')]"):
        bad.drop_tree()
    for br in node.xpath(".//br"):
        br.tail = "\n" + (br.tail or "")
    return node.text_content()


SITE_RULES = {"조선일보": _chosun, "동아일보": _donga}


def _clean(text: str) -> str:
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l and not NOISE.search(l)]
    if lines:
        lines[0] = strip_byline(lines[0])
    return "\n".join(l for l in lines if l)


def fetch_body(link: str, source: str) -> tuple[str | None, str]:
    """(본문 또는 None, 결과 설명). 결과: ok / robots / http_error / too_short"""
    if not allowed(link):
        return None, "robots"
    _wait_turn(urlsplit(link).netloc)
    try:
        res = _client.get(link)
        res.raise_for_status()
    except httpx.HTTPError as e:
        log.info("본문 가져오기 실패 %s: %s", source, e)
        return None, "http_error"
    html = res.text
    rule = SITE_RULES.get(source)
    text = rule(html) if rule else None
    if not text:
        text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True) or ""
    text = _clean(text)
    if len(text) < MIN_CHARS:
        return None, "too_short"
    return text[:MAX_CHARS], "ok"
