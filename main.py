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

# ========== Configuration ==========
load_dotenv()  # loads .env locally; harmless in GitHub Actions

# Required env vars
WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY", "")
EMAIL = os.getenv("EMAIL", "")
EMAIL_PASS = os.getenv("EMAIL_PASS", "")

# Recipients: comma/semicolon/newline separated list
RECIPIENTS_RAW = os.getenv("RECIPIENTS", "")
# Optional: CC/BCC
CC_RAW = os.getenv("CC", "")
BCC_RAW = os.getenv("BCC", "")

# Location / API / SMTP
LAT, LON = 28.4595, 77.0266
LOCATION = "Gurugram – Candor TechSpace (Subhash Chowk)"
WEATHER_API_URL = "https://api.weatherapi.com/v1/forecast.json"  # HTTPS
SMTP_SERVER, SMTP_PORT = "smtp.gmail.com", 465
REQUEST_TIMEOUT = 15
FORECAST_DAYS = 1

# ========== Small helpers ==========
def get_retry_session() -> requests.Session:
    """requests session with simple retry/backoff."""
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.4,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods={"GET"},
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def parse_recipients(csv_like: str) -> List[str]:
    """Split by comma/semicolon/newline, trim spaces, drop empties."""
    tmp = csv_like.replace(";", ",").replace("\n", ",")
    return [p.strip() for p in tmp.split(",") if p.strip()]

def require_env() -> None:
    missing = []
    if not WEATHERAPI_KEY: missing.append("WEATHERAPI_KEY")
    if not EMAIL: missing.append("EMAIL")
    if not EMAIL_PASS: missing.append("EMAIL_PASS")
    # allow RECIPIENTS to be set; if not, fail
    if not (RECIPIENTS_RAW or os.getenv("RECIPIENT")):
        missing.append("RECIPIENTS (or RECIPIENT)")
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

# ========== AQI / UV helpers ==========
def pm25_category(pm25: Optional[float]) -> str:
    if pm25 is None:
        return "Unknown"
    if pm25 <= 30: return "Good"
    if pm25 <= 60: return "Satisfactory"
    if pm25 <= 90: return "Moderate"
    if pm25 <= 120: return "Poor"
    if pm25 <= 250: return "Very Poor"
    return "Severe"

def uv_category(uv: Optional[float]) -> Tuple[str, str]:
    if uv is None:
        return "Unknown", "no UV data"
    if uv < 3:  return "Low", "Minimal risk"
    if uv < 6:  return "Moderate", "Use sunglasses; SPF 30+ if outdoors"
    if uv < 8:  return "High", "SPF 30+, hat, seek shade at midday"
    if uv < 11: return "Very High", "Reduce time in sun 10–16h"
    return "Extreme", "Avoid midday sun; SPF 50+"

# ========== Fetch & format ==========
def fetch_weather() -> Dict[str, Any]:
    session = get_retry_session()
    params = {"key": WEATHERAPI_KEY, "q": f"{LAT},{LON}", "days": FORECAST_DAYS, "aqi": "yes", "alerts": "no"}
    resp = session.get(WEATHER_API_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

def build_email(payload: Dict[str, Any]) -> str:
    tz = payload["location"].get("tz_id") or "Asia/Kolkata"
    now_local = datetime.now(ZoneInfo(tz)).strftime("%d %B %Y, %I:%M %p")
    cur = payload["current"]
    day = payload["forecast"]["forecastday"][0]
    astro, fday = day.get("astro", {}), day.get("day", {})
    aq = cur.get("air_quality", {}) or {}

    # Summary bits
    temp_c, temp_f = cur.get("temp_c"), cur.get("temp_f")
    feels = cur.get("feelslike_c")
    temp_line = (
        " / ".join([f"{temp_c}°C" if temp_c is not None else None,
                    f"{temp_f}°F" if temp_f is not None else None])
    )
    temp_line = " / ".join([p for p in (temp_line.split(" / ") if temp_line else []) if p]) or "N/A"
    if feels is not None:
        temp_line += f" (feels {feels}°C)"

    pm25 = aq.get("pm2_5")
    aqi_line = f"{round(pm25, 1)} μg/m³ – {pm25_category(pm25)}" if pm25 is not None else "No data"

    # Build body
    lines: List[str] = []
    lines.append(f"As of {now_local} in {LOCATION}:")
    lines.append("")
    lines.append(f"• 🌡️ Temperature: {temp_line}")
    lines.append(f"• 🌫️ AQI (PM2.5): {aqi_line}")
    lines.append(f"• 🌅 Sunrise: {astro.get('sunrise') or 'N/A'}   • 🌇 Sunset: {astro.get('sunset') or 'N/A'}")
    lines.append("")
    lines.append("🌤️ Weather")
    lines.append(f"Condition: {cur['condition']['text']}")
    if cur.get("humidity") is not None:   lines.append(f"Humidity: {cur['humidity']}%")
    if cur.get("wind_kph") is not None:   lines.append(f"Wind: {cur['wind_kph']} km/h")
    if cur.get("cloud") is not None:      lines.append(f"Cloud Cover: {cur['cloud']}%")
    if fday.get("daily_chance_of_rain") is not None: lines.append(f"Chance of Rain (today): {fday['daily_chance_of_rain']}%")
    if cur.get("precip_mm") is not None:  lines.append(f"Precipitation (current): {cur['precip_mm']} mm")
    if cur.get("vis_km") is not None:     lines.append(f"Visibility: {cur['vis_km']} km")
    lines.append("")
    lines.append("🌫️ Air Quality")
    if pm25 is not None: lines.append(f"PM2.5: {round(pm25, 1)} μg/m³ – {pm25_category(pm25)}")
    if aq.get("pm10") is not None: lines.append(f"PM10: {round(aq['pm10'], 1)} μg/m³")
    if aq.get("us-epa-index") is not None: lines.append(f"US‑EPA Index: {aq['us-epa-index']} (1=Good … 6=Hazardous)")
    for key in ("co", "no2", "so2", "o3"):
        if aq.get(key) is not None:
            lines.append(f"{key.upper()}: {round(aq[key], 1)}")
    lines.append("")
    uv = cur.get("uv")
    uvc, uv_note = uv_category(uv)
    lines.append("🌞 UV Index")
    lines.append(f"UV: {uv} – {uvc} ({uv_note})")
    lines.append("")
    # Compact concern section
    concerns: List[str] = []
    if pm25 is not None:
        if pm25 > 250: concerns.append("• PM2.5 **Severe** — avoid outdoor exertion; use N95.")
        elif pm25 > 120: concerns.append("• PM2.5 **Very Poor** — limit outdoor time; consider mask.")
        elif pm25 > 90: concerns.append("• PM2.5 **Moderate/Poor** — sensitive groups may feel symptoms.")
    if uv is not None and uv >= 6: concerns.append("• UV **High+** midday — SPF 30+, hat, shade.")
    if cur.get("vis_km") is not None and cur["vis_km"] <= 2: concerns.append("• **Low visibility** — commute with care.")
    if not concerns: concerns.append("• No major flags. Stay hydrated and have a great day!")
    lines.append("⚠️ Concerning Parameters")
    lines.extend(concerns)

    return "\n".join(lines)

# ========== Email ==========
def send_email(subject: str, body: str, to: List[str], cc: Optional[List[str]] = None, bcc: Optional[List[str]] = None) -> None:
    cc = cc or []
    bcc = bcc or []
    all_rcpts = list(dict.fromkeys(to + cc + bcc))  # de-duplicate while preserving order

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Weather Bot", EMAIL))
    msg["To"] = COMMASPACE.join(to)
    if cc: msg["Cc"] = COMMASPACE.join(cc)

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=REQUEST_TIMEOUT) as server:
        server.login(EMAIL, EMAIL_PASS)
        server.sendmail(EMAIL, all_rcpts, msg.as_string())

# ========== Main ==========
def main() -> None:
    require_env()

    # Recipients: prefer RECIPIENTS; fall back to legacy RECIPIENT if defined
    raw = RECIPIENTS_RAW or os.getenv("RECIPIENT", "")
    to = parse_recipients(raw)
    cc = parse_recipients(CC_RAW) if CC_RAW else []
    bcc = parse_recipients(BCC_RAW) if BCC_RAW else []
    if not to:
        raise RuntimeError("No valid email addresses found in RECIPIENTS/RECIPIENT.")

    print(f"Recipients: to={len(to)}, cc={len(cc)}, bcc={len(bcc)}")

    data = fetch_weather()
    email_body = build_email(data)

    tz = data["location"].get("tz_id") or "Asia/Kolkata"
    now_local = datetime.now(ZoneInfo(tz))
    subject = f"Weather & AQI Update • {LOCATION} • {now_local.strftime('%d %b %Y')}"

    print("Sending email...")
    send_email(subject, email_body, to, cc, bcc)
    print("✅ Email sent successfully!")

if __name__ == "__main__":
    main()
