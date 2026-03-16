
import os
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from zoneinfo import ZoneInfo  # Python 3.9+

# ---------- Config ----------
load_dotenv()  # harmless in GitHub Actions; required locally
WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY")
EMAIL = os.getenv("EMAIL")
EMAIL_PASS = os.getenv("EMAIL_PASS")
RECIPIENT = os.getenv("RECIPIENT")

# Use precise coordinates near Subhash Chowk / Candor TechSpace if you like
LAT = 28.4595
LON = 77.0266
LOCATION = "Gurugram – Candor TechSpace (Subhash Chowk)"

# ---------- HTTP retry session ----------
def get_retry_session():
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

# ---------- Helpers ----------
def pm25_category_india(pm25):
    # CPCB-like bands for guidance (μg/m³)
    if pm25 <= 30:
        return "Good"
    elif pm25 <= 60:
        return "Satisfactory"
    elif pm25 <= 90:
        return "Moderate"
    elif pm25 <= 120:
        return "Poor"
    elif pm25 <= 250:
        return "Very Poor"
    else:
        return "Severe"

def uv_category(uv):
    if uv is None:
        return "Unknown", "no UV data"
    if uv < 3:
        return "Low", "Minimal risk"
    elif uv < 6:
        return "Moderate", "Use sunglasses; SPF 30+ if outdoors"
    elif uv < 8:
        return "High", "SPF 30+, hat, seek shade at midday"
    elif uv < 11:
        return "Very High", "Reduce time in sun 10–16h"
    else:
        return "Extreme", "Avoid midday sun; SPF 50+"

# ---------- Fetch ----------
def get_all_data():
    # Single call: current + forecast + AQI + astro
    url = (
        f"http://api.weatherapi.com/v1/forecast.json"
        f"?key={WEATHERAPI_KEY}&q={LAT},{LON}&days=1&aqi=yes&alerts=no"
    )
    session = get_retry_session()
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()

# ---------- Format ----------
def format_weather_email(payload):def format_weather_email(payload):
    tz = payload["location"].get("tz_id") or "Asia/Kolkata"
    now_local = datetime.now(ZoneInfo(tz))
    date_str = now_local.strftime("%d %B %Y, %I:%M %p")

    current = payload["current"]
    day_block = payload["forecast"]["forecastday"][0]
    astro = day_block.get("astro", {})
    day = day_block.get("day", {})

    # Weather
    condition = current["condition"]["text"]
    temp_c = current.get("temp_c")
    temp_f = current.get("temp_f")
    feels_like_c = current.get("feelslike_c")
    humidity = current.get("humidity")
    wind_kmh = current.get("wind_kph")
    cloud_cover = current.get("cloud")
    visibility = current.get("vis_km")
    precip_mm = current.get("precip_mm", 0)
    sunrise = astro.get("sunrise")
    sunset = astro.get("sunset")
    chance_of_rain = day.get("daily_chance_of_rain")  # %

    # AQI / Air quality
    aq = current.get("air_quality", {}) or {}
    pm25 = aq.get("pm2_5")
    pm10 = aq.get("pm10")
    us_epa_idx = aq.get("us-epa-index")  # 1..6
    aqi_cat = pm25_category_india(pm25) if pm25 is not None else "Unknown"

    # UV
    uv_val = current.get("uv")
    uv_cat, uv_note = uv_category(uv_val)

    # Concerning notes (dynamic)
    concerning = []
    if pm25 is not None:
        if pm25 > 250:
            concerning.append("• PM2.5 is in **Severe** range — avoid outdoor exertion; use N95 mask if stepping out.")
        elif pm25 > 120:
            concerning.append("• PM2.5 is **Very Poor** — limit outdoor time; consider a mask.")
        elif pm25 > 90:
            concerning.append("• PM2.5 is **Moderate/Poor** — sensitive groups may feel symptoms.")
    if uv_val is not None and uv_val >= 6:
        concerning.append("• UV is **High or above** around midday — SPF 30+, hat, seek shade.")
    if visibility is not None and visibility <= 2:
        concerning.append("• **Low visibility** — take extra care when commuting this morning.")
    if not concerning:
        concerning.append("• No major flags this morning. Stay hydrated and have a great day!")

    # ---------- Build plain‑text email ----------
    lines = []

    # Header line with timestamp & location
    lines.append(f"As of {date_str} in {LOCATION}:")

    # Quick summary (FIRST FEW LINES)
    # Temperature
    temp_bits = []
    if temp_c is not None:
        temp_bits.append(f"{temp_c}°C")
    if temp_f is not None:
        temp_bits.append(f"{temp_f}°F")
    feels_bit = f" (feels {feels_like_c}°C)" if feels_like_c is not None else ""
    temp_line = " / ".join(temp_bits) + feels_bit if temp_bits else "N/A"

    # AQI (PM2.5)
    aqi_line = f"{round(pm25,1)} μg/m³ – {aqi_cat}" if pm25 is not None else "No data"

    # Sunrise/Sunset
    sunrise_line = sunrise or "N/A"
    sunset_line = sunset or "N/A"

    lines.append(f"• 🌡️ Temperature: {temp_line}")
    lines.append(f"• 🌫️ AQI (PM2.5): {aqi_line}")
    lines.append(f"• 🌅 Sunrise: {sunrise_line}   • 🌇 Sunset: {sunset_line}")
    lines.append("")  # spacer

    # Detail sections
    lines.append("🌤️ Weather")
    lines.append(f"Condition: {condition}")
    if humidity is not None:
        lines.append(f"Humidity: {humidity}%")
    if wind_kmh is not None:
        lines.append(f"Wind: {wind_kmh} km/h")
    if cloud_cover is not None:
        lines.append(f"Cloud Cover: {cloud_cover}%")
    if chance_of_rain is not None:
        lines.append(f"Chance of Rain (today): {chance_of_rain}%")
    if precip_mm is not None:
        lines.append(f"Precipitation (current): {precip_mm} mm")
    if visibility is not None:
        lines.append(f"Visibility: {visibility} km")
    lines.append("")  # spacer

    lines.append("🌫️ Air Quality")
    if pm25 is not None:
        lines.append(f"PM2.5: {round(pm25, 1)} μg/m³ – {aqi_cat}")
    if pm10 is not None:
        lines.append(f"PM10: {round(pm10, 1)} μg/m³")
    if us_epa_idx is not None:
        lines.append(f"US‑EPA Index: {us_epa_idx} (1=Good … 6=Hazardous)")
    for key in ("co", "no2", "so2", "o3"):
        if key in aq and aq[key] is not None:
            lines.append(f"{key.upper()}: {round(aq[key], 1)}")
    lines.append("")  # spacer

    lines.append("🌞 UV Index")
    lines.append(f"UV: {uv_val} – {uv_cat} ({uv_note})")
    lines.append("")  # spacer

    lines.append("⚠️ Concerning Parameters")
    lines.extend(concerning)

    return "\n".join(lines)
# ---------- Email ----------
def send_email(subject, body, to_email):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL
    msg["To"] = to_email

    # SSL port 465 (works with Gmail App Password)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(EMAIL, EMAIL_PASS)
        server.sendmail(EMAIL, [to_email], msg.as_string())

# ---------- Main ----------
def main():
    if not all([WEATHERAPI_KEY, EMAIL, EMAIL_PASS, RECIPIENT]):
        raise RuntimeError("Missing one or more required env vars: WEATHERAPI_KEY, EMAIL, EMAIL_PASS, RECIPIENT")

    data = get_all_data()
    email_body = format_weather_email(data)

    tz = data["location"]["tz_id"] or "Asia/Kolkata"
    now_local = datetime.now(ZoneInfo(tz))
    subject = f"Weather & AQI Update • {LOCATION} • {now_local.strftime('%d %b %Y')}"

    print("Sending email...")
    send_email(subject, email_body, RECIPIENT)
    print("Email sent successfully!")

if __name__ == "__main__":
    main()
