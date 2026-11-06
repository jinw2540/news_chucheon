"""Open-Meteo에서 날씨를 받아 30분 동안 저장해 두고 전달한다."""
import threading
import time

import httpx

CITY = {"name": "서울", "lat": 37.5665, "lon": 126.978}
CACHE_SECONDS = 30 * 60

# WMO 날씨 코드 → 한국어
CODES = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "안개",
    51: "이슬비", 53: "이슬비", 55: "이슬비", 56: "어는 이슬비", 57: "어는 이슬비",
    61: "비", 63: "비", 65: "강한 비", 66: "어는 비", 67: "어는 비",
    71: "눈", 73: "눈", 75: "많은 눈", 77: "싸락눈",
    80: "소나기", 81: "소나기", 82: "강한 소나기", 85: "눈 소나기", 86: "눈 소나기",
    95: "뇌우", 96: "뇌우", 99: "뇌우",
}

_cache: dict = {"at": 0.0, "data": None}
_lock = threading.Lock()


def get_weather() -> dict | None:
    with _lock:
        if _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]
    try:
        res = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": CITY["lat"], "longitude": CITY["lon"],
                "current": "temperature_2m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "Asia/Seoul", "forecast_days": 1,
            },
            timeout=10,
        )
        res.raise_for_status()
        d = res.json()
        data = {
            "city": CITY["name"],
            "temp": round(d["current"]["temperature_2m"]),
            "desc": CODES.get(d["current"]["weather_code"], "날씨 정보"),
            "hi": round(d["daily"]["temperature_2m_max"][0]),
            "lo": round(d["daily"]["temperature_2m_min"][0]),
            "pop": d["daily"]["precipitation_probability_max"][0],
        }
    except Exception:
        return _cache["data"]  # 실패하면 마지막으로 받은 값 (없으면 None)
    with _lock:
        _cache.update(at=time.time(), data=data)
    return data
