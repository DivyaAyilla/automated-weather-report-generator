import os
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import COMMASPACE, formataddr
from typing import Optional, Dict, Any, List, Tuple

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from zoneinfo import ZoneInfo

# ============================================================
# Load environment variables
# ============================================================
load_dotenv()

WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY", "")
EMAIL = os.getenv("EMAIL", "")
EMAIL_PASS = os.getenv("EMAIL_PASS", "")
RECIPIENTS_RAW = os.getenv("RECIPIENTS", "")
CC_RAW = os.getenv("CC", "")
BCC_RAW = os.getenv("BCC", "")

# ============================================================
# Location and API endpoints
# ============================================================
LAT, LON = 17.3967663, 78.3347724
LOCATION = "Hyderabad – Kokapet"

WEATHER_API_URL = "https://api.weatherapi.com/v1/forecast.json"
FORECAST_DAYS = 1
REQUEST_TIMEOUT = 15

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465


# ============================================================
# Helper: Retry-enabled requests session
# ============================================================
def get_retry_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.4,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods={"GET"},
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ============================================================
# Helper: Parse comma/semicolon separated recipients
# ============================================================
def parse_recipients(raw: str) -> List[str]:
    raw = raw.replace(";", ",").replace("\n", ",")
    return [x.strip() for x in raw.split(",") if x.strip()]


# ============================================================
# Validate required environment variables
# ============================================================
def require_env():
    missing = []
    if not WEATHERAPI_KEY:
        missing.append("WEATHERAPI_KEY")
    if not EMAIL:
        missing.append("EMAIL")
    if not EMAIL_PASS:
        missing.append("EMAIL_PASS")
    if not RECIPIENTS_RAW:
        missing.append("RECIPIENTS")

    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


# ============================================================
# AQI + UV helpers
# ============================================================
def pm25_category(pm25: Optional[float]) -> str:
    if pm25 is None: return "Unknown"
    if pm25 <= 30: return "Good"
    if pm25 <= 60: return "Satisfactory"
    if pm25 <= 90: return "Moderate"
    if pm25 <= 120: return "Poor"
    if pm25 <= 250: return "Very Poor"
    return "Severe"


def uv_category(uv: Optional[float]) -> Tuple[str, str]:
    if uv is None: return "Unknown", "No UV data"
    if uv < 3: return "Low", "Minimal risk"
    if uv < 6: return "Moderate", "Use sunglasses, SPF 30+"
    if uv < 8: return "High", "SPF 30+, seek shade"
    if uv < 11: return "Very High", "Avoid sun 10–16h"
    return "Extreme", "SPF 50+, avoid midday sun"


# ============================================================
# Weather Fetcher
# ============================================================
def fetch_weather() -> Dict[str, Any]:
    session = get_retry_session()
    params = {
        "key": WEATHERAPI_KEY,
        "q": f"{LAT},{LON}",
        "days": FORECAST_DAYS,
        "aqi": "yes",
        "alerts": "no"
    }
    resp = session.get(WEATHER_API_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# ============================================================
# Build Email Body
# ============================================================
def build_email(payload: Dict[str, Any]) -> str:
    tz = payload["location"].get("tz_id", "Asia/Kolkata")
    now_local = datetime.now(ZoneInfo(tz)).strftime("%d %B %Y, %I:%M %p")

    cur = payload["current"]
    forecast = payload["forecast"]["forecastday"][0]
    astro = forecast.get("astro", {})
    fday = forecast.get("day", {})
    aq = cur.get("air_quality", {}) or {}

    # Temperature
    temp_c, temp_f = cur.get("temp_c"), cur.get("temp_f")
    feels = cur.get("feelslike_c")
    temp_line = " / ".join(
        p for p in [
            f"{temp_c}°C" if temp_c is not None else None,
            f"{temp_f}°F" if temp_f is not None else None
        ] if p
    )
    if feels is not None:
        temp_line += f" (feels {feels}°C)"

    # AQI
    pm25 = aq.get("pm2_5")
    aqi_line = f"{round(pm25, 1)} μg/m³ – {pm25_category(pm25)}" if pm25 else "No data"

    # Build message
    lines = [
        f"As of {now_local} in {LOCATION}:",
        "",
        f"• 🌡️ Temperature: {temp_line}",
        f"• 🌫️ AQI (PM2.5): {aqi_line}",
        f"• 🌅 Sunrise: {astro.get('sunrise', 'N/A')}   • 🌇 Sunset: {astro.get('sunset', 'N/A')}",
        "",
        "🌤️ Weather",
        f"Condition: {cur['condition']['text']}",
    ]

    optional_fields = [
        ("Humidity", cur.get("humidity"), "%"),
        ("Wind", cur.get("wind_kph"), " km/h"),
        ("Cloud Cover", cur.get("cloud"), "%"),
        ("Chance of Rain (today)", fday.get("daily_chance_of_rain"), "%"),
        ("Precipitation (current)", cur.get("precip_mm"), " mm"),
        ("Visibility", cur.get("vis_km"), " km"),
    ]

    for label, value, unit in optional_fields:
        if value is not None:
            lines.append(f"{label}: {value}{unit}")

    # Air quality
    lines.append("\n🌫️ Air Quality")
    if pm25:
        lines.append(f"PM2.5: {round(pm25,1)} μg/m³ – {pm25_category(pm25)}")

    for key in ("pm10", "us-epa-index", "co", "no2", "so2", "o3"):
        if aq.get(key) is not None:
            lines.append(f"{key.upper()}: {round(aq[key],1)}")

    # UV
    uv = cur.get("uv")
    uv_cat, uv_note = uv_category(uv)
    lines.append("\n🌞 UV Index")
    lines.append(f"UV: {uv} – {uv_cat} ({uv_note})")

    # Safety notes
    concerns = []
    if pm25 and pm25 > 250:
        concerns.append("• PM2.5 **Severe** — avoid outdoor exposure; use N95.")
    elif pm25 and pm25 > 120:
        concerns.append("• PM2.5 **Very Poor** — limit outdoor time.")
    elif pm25 and pm25 > 90:
        concerns.append("• PM2.5 Moderate/Poor — sensitive groups may feel symptoms.")

    if uv and uv >= 6:
        concerns.append("• UV High+ — SPF 30+, shade at midday.")

    vis = cur.get("vis_km")
    if vis is not None and vis <= 2:
        concerns.append("• Low visibility — please commute carefully.")

    if not concerns:
        concerns.append("• No major issues today. Stay hydrated!")

    lines.append("\n⚠️ Concerning Parameters")
    lines.extend(concerns)

    return "\n".join(lines)


# ============================================================
# Send Email
# ============================================================
def send_email(subject: str, body: str, to: List[str], cc=None, bcc=None):
    cc = cc or []
    bcc = bcc or []

    all_rcpts = list(dict.fromkeys(to + cc + bcc))

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Weather Bot", EMAIL))
    msg["To"] = COMMASPACE.join(to)
    if cc:
        msg["Cc"] = COMMASPACE.join(cc)

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=REQUEST_TIMEOUT) as server:
        server.login(EMAIL, EMAIL_PASS)
        server.sendmail(EMAIL, all_rcpts, msg.as_string())


# ============================================================
# Main
# ============================================================
def main():
    require_env()

    to = parse_recipients(RECIPIENTS_RAW)
    cc = parse_recipients(CC_RAW)
    bcc = parse_recipients(BCC_RAW)

    print(f"Recipients: to={len(to)}, cc={len(cc)}, bcc={len(bcc)}")

    data = fetch_weather()
