import time
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
import httpx

app = FastAPI()

# ── Weather ──────────────────────────────────────────────────────────
CITIES = [
    {"name": "Los Angeles",    "lat": 34.0522,  "lon": -118.2437},
    {"name": "Sacramento",     "lat": 38.5816,  "lon": -121.4944},
    {"name": "Citrus Heights", "lat": 38.6924,  "lon": -121.2808},
]

_weather_cache: dict = {"data": None, "ts": 0.0}
WEATHER_TTL = 3600  # 1 hour


@app.get("/api/weather")
async def get_weather():
    now = time.time()
    if _weather_cache["data"] and now - _weather_cache["ts"] < WEATHER_TTL:
        return _weather_cache["data"]

    results = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for city in CITIES:
                r = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": city["lat"],
                        "longitude": city["lon"],
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

    _weather_cache["data"] = results
    _weather_cache["ts"] = now
    return results


# ── Static / root ────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
