import os
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from typing import Optional, Dict, Any, List, Tuple

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from zoneinfo import ZoneInfo

# ---------- Environment Configuration ----------
load_dotenv()  # harmless in GitHub Actions; required locally

WEATHERAPI_KEY: str = os.getenv("WEATHERAPI_KEY", "")
EMAIL: str = os.getenv("EMAIL", "")
EMAIL_PASS: str = os.getenv("EMAIL_PASS", "")
RECIPIENT: str = os.getenv("RECIPIENT", "")

# Location Configuration
LAT: float = 28.4595
LON: float = 77.0266
LOCATION: str = "Gurugram – Candor TechSpace (Subhash Chowk)"

# API Configuration
WEATHER_API_URL: str = "http://api.weatherapi.com/v1/forecast.json"
SMTP_SERVER: str = "smtp.gmail.com"
SMTP_PORT: int = 465
FORECAST_DAYS: int = 1
REQUEST_TIMEOUT: int = 15


# ---------- HTTP Session Management ----------
def get_retry_session() -> requests.Session:
    """Create a requests session with exponential backoff retry strategy."""
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


# ---------- Air Quality & UV Category Classification ----------
def get_pm25_category(pm25: Optional[float]) -> str:
    """
    Classify PM2.5 levels using CPCB-like bands for guidance (μg/m³).
    
    Args:
        pm25: PM2.5 concentration value
        
    Returns:
        Category string (Good, Satisfactory, Moderate, Poor, Very Poor, Severe)
    """
    if pm25 is None:
        return "Unknown"
    
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


def get_uv_category(uv: Optional[float]) -> Tuple[str, str]:
    """
    Classify UV index levels and provide health guidance.
    
    Args:
        uv: UV index value
        
    Returns:
        Tuple of (category, health_note)
    """
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


# ---------- Data Fetching ----------
def fetch_weather_data() -> Dict[str, Any]:
    """
    Fetch current weather, forecast, AQI, and astro data from WeatherAPI.
    
    Returns:
        Dictionary containing weather API response
        
    Raises:
        requests.HTTPError: If API request fails
    """
    params = {
        "key": WEATHERAPI_KEY,
        "q": f"{LAT},{LON}",
        "days": FORECAST_DAYS,
        "aqi": "yes",
        "alerts": "no",
    }
    
    session = get_retry_session()
    response = session.get(WEATHER_API_URL, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    
    return response.json()


# ---------- Data Extraction & Processing ----------
def extract_current_weather(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract current weather conditions from API response."""
    current = payload["current"]
    return {
        "condition": current["condition"]["text"],
        "temp_c": current.get("temp_c"),
        "temp_f": current.get("temp_f"),
        "feels_like_c": current.get("feelslike_c"),
        "humidity": current.get("humidity"),
        "wind_kph": current.get("wind_kph"),
        "cloud_cover": current.get("cloud"),
        "visibility_km": current.get("vis_km"),
        "precip_mm": current.get("precip_mm", 0),
        "uv": current.get("uv"),
    }


def extract_forecast_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract forecast and astronomical data from API response."""
    day_block = payload["forecast"]["forecastday"][0]
    astro = day_block.get("astro", {})
    day = day_block.get("day", {})
    
    return {
        "sunrise": astro.get("sunrise"),
        "sunset": astro.get("sunset"),
        "chance_of_rain": day.get("daily_chance_of_rain"),
    }


def extract_air_quality_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract air quality data from API response."""
    aq = payload["current"].get("air_quality", {}) or {}
    
    return {
        "pm25": aq.get("pm2_5"),
        "pm10": aq.get("pm10"),
        "us_epa_index": aq.get("us-epa-index"),
        "co": aq.get("co"),
        "no2": aq.get("no2"),
        "so2": aq.get("so2"),
        "o3": aq.get("o3"),
    }


def generate_concern_list(
    pm25: Optional[float],
    uv: Optional[float],
    visibility: Optional[float],
) -> List[str]:
    """Generate list of concerning weather parameters."""
    concerns: List[str] = []
    
    if pm25 is not None:
        if pm25 > 250:
            concerns.append(
                "• PM2.5 is in **Severe** range — avoid outdoor exertion; "
                "use N95 mask if stepping out."
            )
        elif pm25 > 120:
            concerns.append(
                "• PM2.5 is **Very Poor** — limit outdoor time; consider a mask."
            )
        elif pm25 > 90:
            concerns.append(
                "• PM2.5 is **Moderate/Poor** — sensitive groups may feel symptoms."
            )
    
    if uv is not None and uv >= 6:
        concerns.append(
            "• UV is **High or above** around midday — SPF 30+, hat, seek shade."
        )
    
    if visibility is not None and visibility <= 2:
        concerns.append(
            "• **Low visibility** — take extra care when commuting this morning."
        )
    
    if not concerns:
        concerns.append(
            "• No major flags this morning. Stay hydrated and have a great day!"
        )
    
    return concerns


# ---------- Email Formatting ----------
def format_weather_email(payload: Dict[str, Any]) -> str:
    """
    Format weather data into a readable email body.
    
    Args:
        payload: Dictionary from weather API response
        
    Returns:
        Formatted email body as string
    """
    # Get timezone and current time
    tz = payload["location"].get("tz_id") or "Asia/Kolkata"
    now_local = datetime.now(ZoneInfo(tz))
    date_str = now_local.strftime("%d %B %Y, %I:%M %p")
    
    # Extract data
    weather = extract_current_weather(payload)
    forecast = extract_forecast_data(payload)
    aqi = extract_air_quality_data(payload)
    
    # Process values
    pm25_category = get_pm25_category(aqi["pm25"])
    uv_category, uv_note = get_uv_category(weather["uv"])
    concerns = generate_concern_list(aqi["pm25"], weather["uv"], weather["visibility_km"])
    
    # Build email lines
    lines: List[str] = []
    
    # Header
    lines.append(f"As of {date_str} in {LOCATION}:")
    lines.append("")
    
    # Quick Summary
    temp_parts = []
    if weather["temp_c"] is not None:
        temp_parts.append(f"{weather['temp_c']}°C")
    if weather["temp_f"] is not None:
        temp_parts.append(f"{weather['temp_f']}°F")
    
    feels_bit = (
        f" (feels {weather['feels_like_c']}°C)"
        if weather["feels_like_c"] is not None
        else ""
    )
    temp_line = " / ".join(temp_parts) + feels_bit if temp_parts else "N/A"
    
    aqi_line = (
        f"{round(aqi['pm25'], 1)} μg/m³ – {pm25_category}"
        if aqi["pm25"] is not None
        else "No data"
    )
    
    sunrise_line = forecast["sunrise"] or "N/A"
    sunset_line = forecast["sunset"] or "N/A"
    
    lines.append(f"• 🌡️ Temperature: {temp_line}")
    lines.append(f"• 🌫️ AQI (PM2.5): {aqi_line}")
    lines.append(f"• 🌅 Sunrise: {sunrise_line}   • 🌇 Sunset: {sunset_line}")
    lines.append("")
    
    # Weather Section
    lines.append("🌤️ Weather")
    lines.append(f"Condition: {weather['condition']}")
    
    if weather["humidity"] is not None:
        lines.append(f"Humidity: {weather['humidity']}%")
    if weather["wind_kph"] is not None:
        lines.append(f"Wind: {weather['wind_kph']} km/h")
    if weather["cloud_cover"] is not None:
        lines.append(f"Cloud Cover: {weather['cloud_cover']}%")
    if forecast["chance_of_rain"] is not None:
        lines.append(f"Chance of Rain (today): {forecast['chance_of_rain']}%")
    if weather["precip_mm"] is not None:
        lines.append(f"Precipitation (current): {weather['precip_mm']} mm")
    if weather["visibility_km"] is not None:
        lines.append(f"Visibility: {weather['visibility_km']} km")
    
    lines.append("")
    
    # Air Quality Section
    lines.append("🌫️ Air Quality")
    
    if aqi["pm25"] is not None:
        lines.append(f"PM2.5: {round(aqi['pm25'], 1)} μg/m³ – {pm25_category}")
    if aqi["pm10"] is not None:
        lines.append(f"PM10: {round(aqi['pm10'], 1)} μg/m³")
    if aqi["us_epa_index"] is not None:
        lines.append(
            f"US‑EPA Index: {aqi['us_epa_index']} (1=Good … 6=Hazardous)"
        )
    
    for pollutant in ["co", "no2", "so2", "o3"]:
        if aqi[pollutant] is not None:
            lines.append(f"{pollutant.upper()}: {round(aqi[pollutant], 1)}")
    
    lines.append("")
    
    # UV Index Section
    lines.append("🌞 UV Index")
    lines.append(f"UV: {weather['uv']} – {uv_category} ({uv_note})")
    lines.append("")
    
    # Concerning Parameters Section
    lines.append("⚠️ Concerning Parameters")
    lines.extend(concerns)
    
    return "\n".join(lines)


# ---------- Email Sending ----------
def send_email(subject: str, body: str, to_email: str) -> None:
    """
    Send an email via Gmail SMTP.
    
    Args:
        subject: Email subject line
        body: Email body content
        to_email: Recipient email address
        
    Raises:
        smtplib.SMTPException: If email sending fails
    """
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL
    msg["To"] = to_email
    
    # SSL port 465 (works with Gmail App Password)
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=REQUEST_TIMEOUT) as server:
        server.login(EMAIL, EMAIL_PASS)
        server.sendmail(EMAIL, [to_email], msg.as_string())


def validate_environment() -> None:
    """
    Validate that all required environment variables are set.
    
    Raises:
        RuntimeError: If any required environment variable is missing
    """
    required_vars = {
        "WEATHERAPI_KEY": WEATHERAPI_KEY,
        "EMAIL": EMAIL,
        "EMAIL_PASS": EMAIL_PASS,
        "RECIPIENT": RECIPIENT,
    }
    
    missing = [name for name, value in required_vars.items() if not value]
    
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


# ---------- Main Execution ----------
def main() -> None:
    """Fetch weather data and send formatted email."""
    validate_environment()
    
    print("Fetching weather data...")
    data = fetch_weather_data()
    
    print("Formatting email...")
    email_body = format_weather_email(data)
    
    # Generate subject line
    tz = data["location"].get("tz_id") or "Asia/Kolkata"
    now_local = datetime.now(ZoneInfo(tz))
    subject = f"Weather & AQI Update • {LOCATION} • {now_local.strftime('%d %b %Y')}"
    
    print("Sending email...")
    send_email(subject, email_body, RECIPIENT)
    print("✅ Email sent successfully!")


if __name__ == "__main__":
    main()
