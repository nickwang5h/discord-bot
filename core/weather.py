import asyncio
import logging
from typing import Any
from urllib.parse import quote

import aiohttp
import discord

logger = logging.getLogger(__name__)

I18N = {
    "en": {
        "snow_tip": "❄️ Tip: High chance of snow today. Keep warm and beware of slippery roads!",
        "rain_tip": "☔ Tip: High chance of rain today. Don't forget your umbrella!",
        "hot_tip": "🔥 Tip: It's going to be hot. Stay hydrated and cool!",
        "uv_tip": "🕶️ Tip: High UV index today. Don't forget sunscreen!",
        "cold_tip": "🧥 Tip: Cold weather or large temperature difference today. Dress warmly!",
        "nice_tip": "🌤️ Nice weather today. Have a great day!",
        "weather_today": "📍 {city} Weather Today",
        "temp_feels_like": "🌡️ Temperature & Feels Like",
        "temp_value": "{min}°C ~ {max}°C (Max Feels Like {feels}°C)",
        "conditions": "☀️ Conditions",
        "conditions_value": "{cond} (UV Index: {uv})",
        "precip_chance": "💧 Precipitation Chance",
        "precip_value": "{chance}% (Approx. {precip:.1f}mm)",
        "tomorrow_forecast": "🔮 Tomorrow's Forecast",
        "tomorrow_value": "{cond}, {min}°C ~ {max}°C",
        "tips": "💡 Tips",
        "failed_title": "📍 Failed to fetch weather for {city}",
        "morning_greet": "🌤️ **Good morning! Here is today's weather forecast:**",
        "alert_smoke": "⚠️ ALERT: Smoke/Wildfire detected. Air quality may be poor!",
        "alert_freezing_rain": "⚠️ ALERT: Freezing rain. Roads will be extremely slippery!",
        "alert_storm": "⚠️ ALERT: Severe storm/thunderstorm approaching.",
        "alert_blizzard": "⚠️ ALERT: Blizzard/Heavy snow conditions.",
    },
    "zh": {
        "snow_tip": "❄️ 提醒：今天大概率会下雪，出门注意防寒防滑！",
        "rain_tip": "☔ 提醒：今天大概率会下雨，出门别忘了带伞哦！",
        "hot_tip": "🔥 提醒：天气炎热，注意防暑降温！",
        "uv_tip": "🕶️ 提醒：今天紫外线较强，注意防晒！",
        "cold_tip": "🧥 提醒：气温较低或昼夜温差大，注意保暖！",
        "nice_tip": "🌤️ 今天气温适宜，是不错的一天！",
        "weather_today": "📍 {city} 今日天气",
        "temp_feels_like": "🌡️ 气温与体感",
        "temp_value": "{min}°C ~ {max}°C (最高体感 {feels}°C)",
        "conditions": "☀️ 天气状况",
        "conditions_value": "{cond} (UV指数: {uv})",
        "precip_chance": "💧 降水概率",
        "precip_value": "{chance}% (约 {precip:.1f}mm)",
        "tomorrow_forecast": "🔮 明日预报",
        "tomorrow_value": "{cond}, {min}°C ~ {max}°C",
        "tips": "💡 出行建议",
        "failed_title": "📍 {city} 天气获取失败",
        "morning_greet": "🌤️ **早安！今日天气播报：**",
        "alert_smoke": "⚠️ 警报：检测到烟尘/山火！空气质量可能很差，请注意防范！",
        "alert_freezing_rain": "⚠️ 警报：冻雨天气！道路将极度结冰湿滑，请小心出行！",
        "alert_storm": "⚠️ 警报：暴风雨/雷暴天气即将来临，请注意安全！",
        "alert_blizzard": "⚠️ 警报：暴风雪/大雪天气，视线不佳且路面积雪，请尽量减少外出！",
    },
}

WMO_CODES = {
    "en": {
        0: "Clear sky",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Fog",
        48: "Depositing rime fog",
        51: "Light drizzle",
        53: "Moderate drizzle",
        55: "Dense drizzle",
        56: "Light freezing drizzle",
        57: "Dense freezing drizzle",
        61: "Slight rain",
        63: "Moderate rain",
        65: "Heavy rain",
        66: "Light freezing rain",
        67: "Heavy freezing rain",
        71: "Slight snow fall",
        73: "Moderate snow fall",
        75: "Heavy snow fall",
        77: "Snow grains",
        80: "Slight rain showers",
        81: "Moderate rain showers",
        82: "Violent rain showers",
        85: "Slight snow showers",
        86: "Heavy snow showers",
        95: "Thunderstorm",
        96: "Thunderstorm with slight hail",
        99: "Thunderstorm with heavy hail",
    },
    "zh": {
        0: "晴朗",
        1: "大部晴朗",
        2: "局部多云",
        3: "阴天",
        45: "有雾",
        48: "雾凇",
        51: "小毛毛雨",
        53: "中毛毛雨",
        55: "大毛毛雨",
        56: "小冻毛毛雨",
        57: "大冻毛毛雨",
        61: "小雨",
        63: "中雨",
        65: "大雨",
        66: "小冻雨",
        67: "大冻雨",
        71: "小雪",
        73: "中雪",
        75: "大雪",
        77: "米雪",
        80: "小阵雨",
        81: "中阵雨",
        82: "强阵雨",
        85: "小阵雪",
        86: "大阵雪",
        95: "雷暴",
        96: "雷暴伴有小冰雹",
        99: "雷暴伴有大冰雹",
    },
}

COLOR_SEVERE = 0xE74C3C
COLOR_CAUTION = 0xF1C40F
COLOR_NICE = 0x2ECC71
COLOR_RAIN = 0x3498DB
COLOR_SNOW = 0xECF0F1
COLOR_CLOUDY = 0x95A5A6


def get_text(lang: str, key: str, **kwargs: Any) -> str:
    text_dict = I18N.get(lang, I18N["zh"])
    text = text_dict.get(key, I18N["en"].get(key, key))
    if kwargs:
        return text.format(**kwargs)
    return text


def get_wmo_text(lang: str, code: int) -> str:
    lang_dict = WMO_CODES.get(lang, WMO_CODES["en"])
    return lang_dict.get(code, WMO_CODES["en"].get(code, "Unknown"))


async def fetch_weather_wttr(city: str, session: aiohttp.ClientSession, lang: str = "zh") -> discord.Embed:
    encoded_city = quote(city.strip())
    url = f"https://wttr.in/{encoded_city}?format=j1"
    if lang == "zh":
        url += "&lang=zh"

    timeout = aiohttp.ClientTimeout(total=12)
    async with session.get(url, timeout=timeout) as response:
        response.raise_for_status()
        data = await response.json(content_type=None)

    today = data["weather"][0]
    tomorrow = data["weather"][1]
    hourly = today["hourly"]

    precip = sum(float(h.get("precipMM", 0)) for h in hourly)

    def get_max_precip(hourly_data: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        def max_chance(h: dict[str, Any]) -> int:
            return max(int(h.get("chanceofrain", 0)), int(h.get("chanceofsnow", 0)))

        max_hour = max(hourly_data, key=max_chance)
        return max_chance(max_hour), max_hour

    chance_of_precip, max_precip_hour = get_max_precip(hourly)

    def get_desc(hour_data: dict[str, Any]) -> str:
        if lang == "zh" and "lang_zh" in hour_data:
            return hour_data["lang_zh"][0]["value"]
        return hour_data["weatherDesc"][0]["value"]

    if chance_of_precip >= 30:
        conditions_en = max_precip_hour["weatherDesc"][0]["value"]
        conditions_display = get_desc(max_precip_hour)
    else:
        conditions_en = hourly[4]["weatherDesc"][0]["value"] if len(hourly) > 4 else hourly[0]["weatherDesc"][0]["value"]
        conditions_display = get_desc(hourly[4] if len(hourly) > 4 else hourly[0])

    tomorrow_chance, tomorrow_max_hour = get_max_precip(tomorrow["hourly"])
    if tomorrow_chance >= 30:
        tomorrow_cond_display = get_desc(tomorrow_max_hour)
    else:
        tomorrow_cond_display = get_desc(tomorrow["hourly"][4] if len(tomorrow["hourly"]) > 4 else tomorrow["hourly"][0])

    feels_like = max(int(h.get("FeelsLikeC", 0)) for h in hourly)
    uv_index = int(today.get("uvIndex", 0))

    tomorrow_min = tomorrow.get("mintempC", "-")
    tomorrow_max = tomorrow.get("maxtempC", "-")

    alert_tips: list[str] = []
    is_severe = False
    all_conds_str = " ".join([h["weatherDesc"][0]["value"].lower() for h in hourly])

    if any(k in all_conds_str for k in ("smoke", "haze", "wildfire")):
        alert_tips.append(get_text(lang, "alert_smoke"))
        is_severe = True
    if any(k in all_conds_str for k in ("freezing", "ice")):
        alert_tips.append(get_text(lang, "alert_freezing_rain"))
        is_severe = True
    if any(k in all_conds_str for k in ("storm", "thunder", "torrential")):
        alert_tips.append(get_text(lang, "alert_storm"))
        is_severe = True
    if any(k in all_conds_str for k in ("blizzard", "heavy snow")):
        alert_tips.append(get_text(lang, "alert_blizzard"))
        is_severe = True

    tips: list[str] = []
    is_caution = False
    if chance_of_precip > 50 or precip > 2.0:
        is_caution = True
        if int(max_precip_hour.get("chanceofsnow", 0)) > 50:
            tips.append(get_text(lang, "snow_tip"))
        else:
            tips.append(get_text(lang, "rain_tip"))

    if int(today.get("maxtempC", 0)) >= 30 or feels_like >= 35:
        is_caution = True
        tips.append(get_text(lang, "hot_tip"))
    if uv_index > 5:
        is_caution = True
        tips.append(get_text(lang, "uv_tip"))
    if int(today.get("mintempC", 0)) < 5 or (int(today.get("maxtempC", 0)) - int(today.get("mintempC", 0)) > 15):
        is_caution = True
        tips.append(get_text(lang, "cold_tip"))

    all_tips = alert_tips + tips
    tip_text = "\n".join(all_tips) if all_tips else get_text(lang, "nice_tip")

    if is_severe:
        color = COLOR_SEVERE
    elif is_caution:
        if chance_of_precip > 50 or precip > 1.0:
            if "snow" in conditions_en.lower() or int(max_precip_hour.get("chanceofsnow", 0)) > 50:
                color = COLOR_SNOW
            else:
                color = COLOR_RAIN
        else:
            color = COLOR_CAUTION
    elif "cloud" in conditions_en.lower() or "overcast" in conditions_en.lower():
        color = COLOR_CLOUDY
    else:
        color = COLOR_NICE

    embed = discord.Embed(
        title=get_text(lang, "weather_today", city=city),
        color=discord.Color(color),
    )
    embed.add_field(
        name=get_text(lang, "temp_feels_like"),
        value=get_text(lang, "temp_value", min=today.get("mintempC", "-"), max=today.get("maxtempC", "-"), feels=feels_like),
        inline=True,
    )
    embed.add_field(
        name=get_text(lang, "conditions"),
        value=get_text(lang, "conditions_value", cond=conditions_display, uv=uv_index),
        inline=True,
    )
    embed.add_field(
        name=get_text(lang, "precip_chance"),
        value=get_text(lang, "precip_value", chance=chance_of_precip, precip=precip),
        inline=True,
    )
    embed.add_field(
        name=get_text(lang, "tomorrow_forecast"),
        value=get_text(lang, "tomorrow_value", cond=tomorrow_cond_display, min=tomorrow_min, max=tomorrow_max),
        inline=False,
    )
    embed.add_field(
        name=get_text(lang, "tips"),
        value=tip_text,
        inline=False,
    )
    embed.set_footer(text="Data provided by wttr.in")
    return embed


async def fetch_weather_open_meteo(city: str, session: aiohttp.ClientSession, lang: str = "zh") -> discord.Embed:
    encoded_city = quote(city.strip())
    geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded_city}&count=1&language=en&format=json"
    timeout = aiohttp.ClientTimeout(total=12)

    async with session.get(geo_url, timeout=timeout) as geo_resp:
        geo_resp.raise_for_status()
        geo_data = await geo_resp.json(content_type=None)

    results = geo_data.get("results")
    if not results:
        raise ValueError(f"City '{city}' not found on Open-Meteo.")

    lat = results[0]["latitude"]
    lon = results[0]["longitude"]

    weather_url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,apparent_temperature,precipitation_probability,weather_code"
        f"&daily=weather_code,temperature_2m_max,temperature_2m_min,uv_index_max,precipitation_probability_max,precipitation_sum"
        f"&timezone=auto"
    )
    async with session.get(weather_url, timeout=timeout) as response:
        response.raise_for_status()
        data = await response.json(content_type=None)

    hourly = data["hourly"]
    daily = data["daily"]

    today_max_temp = int(daily["temperature_2m_max"][0])
    today_min_temp = int(daily["temperature_2m_min"][0])
    today_uv = int(daily["uv_index_max"][0]) if daily.get("uv_index_max") and daily["uv_index_max"][0] is not None else 0
    today_precip_chance = daily["precipitation_probability_max"][0] if daily.get("precipitation_probability_max") and daily["precipitation_probability_max"][0] is not None else 0
    today_precip_sum = float(daily["precipitation_sum"][0]) if daily.get("precipitation_sum") and daily["precipitation_sum"][0] is not None else 0.0
    today_weather_code = daily["weather_code"][0]

    tomorrow_max_temp = int(daily["temperature_2m_max"][1]) if len(daily["temperature_2m_max"]) > 1 else today_max_temp
    tomorrow_min_temp = int(daily["temperature_2m_min"][1]) if len(daily["temperature_2m_min"]) > 1 else today_min_temp
    tomorrow_weather_code = daily["weather_code"][1] if len(daily["weather_code"]) > 1 else today_weather_code

    apparent_temps = hourly.get("apparent_temperature", [])[:24]
    feels_like = int(max(apparent_temps)) if apparent_temps else today_max_temp

    conditions_display = get_wmo_text(lang, today_weather_code)
    tomorrow_cond_display = get_wmo_text(lang, tomorrow_weather_code)

    alert_tips: list[str] = []
    is_severe = False

    if today_weather_code in [56, 57, 66, 67]:
        alert_tips.append(get_text(lang, "alert_freezing_rain"))
        is_severe = True
    if today_weather_code in [95, 96, 99]:
        alert_tips.append(get_text(lang, "alert_storm"))
        is_severe = True
    if today_weather_code in [75, 86]:
        alert_tips.append(get_text(lang, "alert_blizzard"))
        is_severe = True

    tips: list[str] = []
    is_caution = False
    if today_precip_chance > 50 or today_precip_sum > 2.0:
        is_caution = True
        if today_weather_code in [71, 73, 75, 77, 85, 86]:
            tips.append(get_text(lang, "snow_tip"))
        else:
            tips.append(get_text(lang, "rain_tip"))

    if today_max_temp >= 30 or feels_like >= 35:
        is_caution = True
        tips.append(get_text(lang, "hot_tip"))
    if today_uv > 5:
        is_caution = True
        tips.append(get_text(lang, "uv_tip"))
    if today_min_temp < 5 or (today_max_temp - today_min_temp > 15):
        is_caution = True
        tips.append(get_text(lang, "cold_tip"))

    all_tips = alert_tips + tips
    tip_text = "\n".join(all_tips) if all_tips else get_text(lang, "nice_tip")

    if is_severe:
        color = COLOR_SEVERE
    elif is_caution:
        if today_precip_chance > 50 or today_precip_sum > 1.0:
            if today_weather_code in [71, 73, 75, 77, 85, 86]:
                color = COLOR_SNOW
            else:
                color = COLOR_RAIN
        else:
            color = COLOR_CAUTION
    elif today_weather_code in [1, 2, 3, 45, 48]:
        color = COLOR_CLOUDY
    else:
        color = COLOR_NICE

    embed = discord.Embed(
        title=get_text(lang, "weather_today", city=city),
        color=discord.Color(color),
    )
    embed.add_field(
        name=get_text(lang, "temp_feels_like"),
        value=get_text(lang, "temp_value", min=today_min_temp, max=today_max_temp, feels=feels_like),
        inline=True,
    )
    embed.add_field(
        name=get_text(lang, "conditions"),
        value=get_text(lang, "conditions_value", cond=conditions_display, uv=today_uv),
        inline=True,
    )
    embed.add_field(
        name=get_text(lang, "precip_chance"),
        value=get_text(lang, "precip_value", chance=today_precip_chance, precip=today_precip_sum),
        inline=True,
    )
    embed.add_field(
        name=get_text(lang, "tomorrow_forecast"),
        value=get_text(lang, "tomorrow_value", cond=tomorrow_cond_display, min=tomorrow_min_temp, max=tomorrow_max_temp),
        inline=False,
    )
    embed.add_field(
        name=get_text(lang, "tips"),
        value=tip_text,
        inline=False,
    )
    embed.set_footer(text="Data provided by Open-Meteo (Backup)")
    return embed


async def get_weather_embed(city: str, session: aiohttp.ClientSession, lang: str = "zh") -> discord.Embed:
    try:
        return await fetch_weather_wttr(city, session, lang=lang)
    except Exception as primary_error:
        logger.warning("wttr.in 查询城市 %s 失败: %s，尝试切换 Open-Meteo 备用 API", city, primary_error)
        try:
            return await fetch_weather_open_meteo(city, session, lang=lang)
        except Exception as fallback_error:
            logger.error("城市 %s 天气备用接口也失败: %s", city, fallback_error)
            embed = discord.Embed(
                title=get_text(lang, "failed_title", city=city),
                description=f"wttr.in Error: {primary_error}\nOpen-Meteo Error: {fallback_error}",
                color=discord.Color(COLOR_SEVERE),
            )
            return embed


async def build_morning_weather_embeds(
    cities: list[str],
    session: aiohttp.ClientSession,
    lang: str = "zh",
) -> list[discord.Embed]:
    tasks = [get_weather_embed(city, session, lang=lang) for city in cities if city.strip()]
    return list(await asyncio.gather(*tasks))
