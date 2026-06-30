import asyncio
import os
import time
from itertools import zip_longest

import feedparser
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

YOUTUBE_KEY = os.getenv("YOUTUBE_API_KEY", "")
NEWS_KEY    = os.getenv("NEWSAPI_KEY", "")
BROWSER_UA  = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_cache: dict = {
    "weather": {"data": None, "ts": 0.0},
    "beauty":  {"data": None, "ts": 0.0},
    "news":    {"data": None, "ts": 0.0},
    "housing": {"data": None, "ts": 0.0},
}
WEATHER_TTL = 3_600    # 1 hour
BEAUTY_TTL  = 43_200   # 12 hours
NEWS_TTL    = 7_200    # 2 hours
HOUSING_TTL = 43_200   # 12 hours

app = FastAPI()


# ── helpers ──────────────────────────────────────────────────────────

async def yt_search(client: httpx.AsyncClient, query: str, n: int = 6) -> list:
    if not YOUTUBE_KEY:
        return []
    try:
        r = await client.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet", "q": query, "type": "video",
                "order": "date", "maxResults": n, "key": YOUTUBE_KEY,
            },
        )
        r.raise_for_status()
        return [
            {
                "title": it["snippet"]["title"],
                "thumbnail": it["snippet"]["thumbnails"]["medium"]["url"],
                "url": f"https://www.youtube.com/watch?v={it['id']['videoId']}",
                "source": "youtube",
                "channel": it["snippet"]["channelTitle"],
            }
            for it in r.json().get("items", [])
            if it.get("id", {}).get("videoId")
        ]
    except Exception:
        return []


async def news_search(client: httpx.AsyncClient, query: str, n: int = 4) -> list:
    if not NEWS_KEY:
        return []
    try:
        r = await client.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query, "sortBy": "publishedAt",
                "pageSize": n, "language": "en", "apiKey": NEWS_KEY,
            },
        )
        r.raise_for_status()
        return [
            {
                "title": a["title"],
                "thumbnail": a.get("urlToImage") or "",
                "url": a["url"],
                "source": "newsapi",
                "channel": (a.get("source") or {}).get("name", ""),
            }
            for a in r.json().get("articles", [])
            if a.get("title") and a.get("url")
            and "[Removed]" not in (a.get("title") or "")
        ]
    except Exception:
        return []


async def reddit_hot(client: httpx.AsyncClient, sub: str, n: int = 5) -> list:
    try:
        r = await client.get(
            f"https://www.reddit.com/r/{sub}/hot.json",
            params={"limit": n + 4},
            headers={"User-Agent": "HollywoodDashboard/1.0"},
        )
        r.raise_for_status()
        items = []
        for post in r.json()["data"]["children"]:
            d = post["data"]
            if d.get("stickied") or d.get("over_18"):
                continue
            thumb = d.get("thumbnail", "")
            if not (thumb and thumb.startswith("http")):
                preview = d.get("preview", {}).get("images", [])
                thumb = preview[0]["source"]["url"].replace("&amp;", "&") if preview else ""
            items.append({
                "title": d["title"],
                "thumbnail": thumb,
                "url": f"https://www.reddit.com{d['permalink']}",
                "source": "reddit",
                "channel": f"r/{sub}",
            })
            if len(items) >= n:
                break
        return items
    except Exception:
        return []


async def fetch_rss(client: httpx.AsyncClient, url: str, n: int = 8, ua=None) -> list:
    try:
        r = await client.get(
            url,
            headers={"User-Agent": ua or "HollywoodDashboard/1.0"},
            follow_redirects=True,
            timeout=10,
        )
        r.raise_for_status()
        feed = feedparser.parse(r.text)
        return [
            {
                "title":     e.get("title", ""),
                "url":       e.get("link", ""),
                "published": e.get("published", ""),
            }
            for e in feed.entries[:n]
            if e.get("title") and e.get("link")
        ]
    except Exception:
        return []


# ── Weather ──────────────────────────────────────────────────────────

CITIES = [
    {"name": "Los Angeles",    "lat": 34.0522,  "lon": -118.2437},
    {"name": "Sacramento",     "lat": 38.5816,  "lon": -121.4944},
    {"name": "Citrus Heights", "lat": 38.6924,  "lon": -121.2808},
]


@app.get("/api/weather")
async def get_weather():
    now = time.time()
    if _cache["weather"]["data"] and now - _cache["weather"]["ts"] < WEATHER_TTL:
        return _cache["weather"]["data"]

    results = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for city in CITIES:
                r = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": city["lat"], "longitude": city["lon"],
                        "current": "temperature_2m,weathercode,windspeed_10m",
                        "daily": "temperature_2m_max,temperature_2m_min",
                        "timezone": "America/Los_Angeles",
                        "temperature_unit": "fahrenheit",
                        "forecast_days": 1,
                    },
                )
                r.raise_for_status()
                d = r.json()
                results.append({
                    "city": city["name"],
                    "temp": round(d["current"]["temperature_2m"]),
                    "code": d["current"]["weathercode"],
                    "hi":   round(d["daily"]["temperature_2m_max"][0]),
                    "lo":   round(d["daily"]["temperature_2m_min"][0]),
                })
    except Exception:
        return JSONResponse(status_code=503, content={"error": "weather unavailable"})

    _cache["weather"]["data"] = results
    _cache["weather"]["ts"] = now
    return results


# ── Beauty & Style ────────────────────────────────────────────────────

@app.get("/api/beauty")
async def get_beauty():
    now = time.time()
    if _cache["beauty"]["data"] and now - _cache["beauty"]["ts"] < BEAUTY_TTL:
        return _cache["beauty"]["data"]

    async with httpx.AsyncClient(timeout=15) as client:
        (
            skin_yt, skin_news,
            hair_yt, hair_reddit,
            fashion_yt, fashion_news, fashion_reddit,
            lux_yt1, lux_yt2, lux_reddit,
        ) = await asyncio.gather(
            yt_search(client, "skincare new products 2026", 5),
            news_search(client, "skincare trends new products 2026", 3),
            yt_search(client, "Black women hairstyles 2026 trending", 6),
            reddit_hot(client, "BlackHair", 4),
            yt_search(client, "fashion fabric trends 2026", 5),
            news_search(client, "fashion fabric trends 2026", 3),
            reddit_hot(client, "femalefashionadvice", 3),
            yt_search(client, "coastal luxury fashion aesthetic 2026", 4),
            yt_search(client, "quiet luxury outfit ideas designer 2026", 4),
            reddit_hot(client, "OldMoney", 3),
        )

    data = {
        "skincare":   skin_yt   + skin_news,
        "hairstyles": hair_yt   + hair_reddit,
        "fashion":    fashion_yt + fashion_news + fashion_reddit,
        "luxury":     lux_yt1   + lux_yt2 + lux_reddit,
    }

    _cache["beauty"]["data"] = data
    _cache["beauty"]["ts"] = now
    return data


# ── News ──────────────────────────────────────────────────────────────

@app.get("/api/news")
async def get_news():
    now = time.time()
    if _cache["news"]["data"] and now - _cache["news"]["ts"] < NEWS_TTL:
        return _cache["news"]["data"]

    async with httpx.AsyncClient(timeout=15) as client:
        sacramento, essence, black_ent, national = await asyncio.gather(
            fetch_rss(client, "https://fox40.com/feed/"),
            fetch_rss(client, "https://www.essence.com/feed/"),
            fetch_rss(client, "https://www.blackenterprise.com/feed/"),
            fetch_rss(client, "https://feeds.npr.org/1001/rss.xml"),
        )

    essence_tagged   = [{"source_name": "Essence",          **i} for i in essence]
    black_ent_tagged = [{"source_name": "Black Enterprise", **i} for i in black_ent]
    black_media = [
        item
        for pair in zip_longest(essence_tagged, black_ent_tagged)
        for item in pair
        if item
    ]

    data = {
        "sacramento":  sacramento,
        "black_media": black_media[:10],
        "national":    national[:8],
    }

    _cache["news"]["data"] = data
    _cache["news"]["ts"] = now
    return data


# ── Housing ───────────────────────────────────────────────────────────

@app.get("/api/housing")
async def get_housing():
    now = time.time()
    if _cache["housing"]["data"] and now - _cache["housing"]["ts"] < HOUSING_TTL:
        return _cache["housing"]["data"]

    async with httpx.AsyncClient(timeout=15) as client:
        sac, ca, la = await asyncio.gather(
            news_search(client, "Sacramento housing market real estate 2026", 5),
            news_search(client, "California housing market real estate 2026", 5),
            news_search(client, "Los Angeles housing market real estate 2026", 5),
        )

    data = {"sacramento": sac, "california": ca, "los_angeles": la}
    _cache["housing"]["data"] = data
    _cache["housing"]["ts"] = now
    return data


# ── Static / root ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
