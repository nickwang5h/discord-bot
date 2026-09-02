import asyncio
import logging
from typing import Any
from urllib.parse import quote

import aiohttp
import discord

logger = logging.getLogger(__name__)

CITY_ALIASES = {
    "渥太华": "Ottawa",
    "萨德伯里": "Sudbury",
    "多伦多": "Toronto",
    "滑铁卢": "Waterloo",
    "蒙特利尔": "Montreal",
    "温哥华": "Vancouver",
    "卡尔加里": "Calgary",
    "埃德蒙顿": "Edmonton",
    "魁北克": "Quebec",
    "哈利法克斯": "Halifax",
    "维多利亚": "Victoria",
    "伦敦": "London,Ontario",
    "温莎": "Windsor,Ontario",
    "金斯顿": "Kingston,Ontario",
    "万锦": "Markham",
    "列治文山": "Richmond Hill",
    "密西沙加": "Mississauga",
    "北京": "Beijing",
    "上海": "Shanghai",
    "广州": "Guangzhou",
    "深圳": "Shenzhen",
    "杭州": "Hangzhou",
    "成都": "Chengdu",
    "武汉": "Wuhan",
    "南京": "Nanjing",
    "香港": "Hong Kong",
    "台北": "Taipei",
}

WWO_CODES = {
    113: "晴朗",
    116: "局部多云",
    119: "多云",
    122: "阴天",
    143: "薄雾",
    176: "局部小阵雨",
    179: "局部小阵雪",
    182: "局部冰雹",
    185: "局部冻毛毛雨",
    200: "雷雨天气",
    227: "风吹雪",
    230: "暴风雪",
    248: "大雾",
    260: "冻雾",
    263: "局部小毛毛雨",
    266: "小毛毛雨",
    281: "冻毛毛雨",
    284: "强冻毛毛雨",
    293: "局部小雨",
    296: "小雨",
    299: "中阵雨",
    302: "中雨",
    305: "中大雨",
    308: "大雨",
    311: "小冻雨",
    314: "强冻雨",
    317: "小冰雹",
    320: "中大冰雹",
    323: "局部小雪",
    326: "小雪",
    329: "局部中雪",
    332: "中雪",
    335: "局部大雪",
    338: "暴雪",
    350: "冰粒",
    353: "小阵雨",
    356: "中阵雨",
    359: "暴雨",
    362: "小冰雪阵雨",
    365: "中冰雪阵雨",
    368: "小阵雪",
    371: "大阵雪",
    374: "小冰粒阵雨",
    377: "中大冰粒阵雨",
    386: "雷阵雨伴有冰雹",
    389: "强雷雨",
    392: "雷阵雪伴有冰雹",
    395: "强雷阵雪",
}

DESC_TRANSLATIONS = {
    "clear": "晴朗",
    "sunny": "晴朗",
    "partly cloudy": "局部多云",
    "cloudy": "多云",
    "overcast": "阴天",
    "mist": "薄雾",
    "fog": "大雾",
    "freezing fog": "冻雾",
    "light rain shower": "小阵雨",
    "moderate or heavy rain shower": "中大阵雨",
    "torrential rain shower": "暴雨",
    "light rain": "小雨",
    "moderate rain": "中雨",
    "heavy rain": "大雨",
    "patchy rain possible": "局部有雨",
    "patchy rain nearby": "局部有小阵雨",
    "light snow": "小雪",
    "moderate snow": "中雪",
    "heavy snow": "大雪",
    "blizzard": "暴风雪",
    "blowing snow": "风吹雪",
    "light freezing rain": "小冻雨",
    "moderate or heavy freezing rain": "强冻雨",
    "thundery outbreaks possible": "局部有雷暴",
    "moderate or heavy rain with thunder": "强雷雨",
    "patchy light rain with thunder": "雷阵雨",
}

WMO_CODES = {
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
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "中阵雨",
    82: "暴雨",
    85: "小阵雪",
    86: "大阵雪",
    95: "雷暴",
    96: "雷暴伴有小冰雹",
    99: "强雷暴伴有大冰雹",
}

COLOR_SEVERE = 0xE74C3C
COLOR_CAUTION = 0xF39C12
COLOR_NICE = 0x2ECC71
COLOR_RAIN = 0x3498DB
COLOR_SNOW = 0x5DADE2
COLOR_CLOUDY = 0x95A5A6


def resolve_city_name(city: str) -> tuple[str, str]:
    trimmed = city.strip()
    if not trimmed:
        return "Ottawa", "Ottawa"
    if trimmed in CITY_ALIASES:
        return CITY_ALIASES[trimmed], trimmed
    for alias, target in CITY_ALIASES.items():
        if alias in trimmed:
            return target, trimmed
    return trimmed, trimmed


def translate_condition(code: int | None, raw_desc: str) -> str:
    if code is not None and code in WWO_CODES:
        return WWO_CODES[code]
    normalized = raw_desc.strip().lower()
    if normalized in DESC_TRANSLATIONS:
        return DESC_TRANSLATIONS[normalized]
    for eng, zh in DESC_TRANSLATIONS.items():
        if eng in normalized:
            return zh
    return raw_desc.strip() or "未知天气"


def format_precip_window_wttr(hourly: list[dict[str, Any]]) -> str:
    rainy_slots: list[int] = []
    for h in hourly:
        t = int(h.get("time", "0"))
        chance = max(int(h.get("chanceofrain", 0)), int(h.get("chanceofsnow", 0)))
        precip = float(h.get("precipMM", 0.0))
        if chance >= 40 or precip >= 0.3:
            rainy_slots.append(t)
    if not rainy_slots:
        return ""

    names: list[str] = []
    if any(t in (0, 300) for t in rainy_slots):
        names.append("夜间")
    if any(t in (600, 900) for t in rainy_slots):
        names.append("上午")
    if any(t in (1200, 1500) for t in rainy_slots):
        names.append("下午")
    if any(t in (1800, 2100) for t in rainy_slots):
        names.append("晚间")

    if len(names) >= 3:
        return "全天分散有降水"
    if len(names) == 2:
        return f"{names[0]}至{names[1]}"
    return f"{names[0]}"


async def fetch_weather_wttr(city: str, session: aiohttp.ClientSession, lang: str = "zh") -> discord.Embed:
    search_query, display_label = resolve_city_name(city)
    encoded_city = quote(search_query)
    url = f"https://wttr.in/{encoded_city}?format=j1"

    timeout = aiohttp.ClientTimeout(total=8)
    async with session.get(url, timeout=timeout) as response:
        response.raise_for_status()
        data = await response.json(content_type=None)

    today = data["weather"][0]
    tomorrow = data["weather"][1]
    hourly = today["hourly"]

    min_temp = int(today.get("mintempC", 0))
    max_temp = int(today.get("maxtempC", 0))
    min_feels = min(int(h.get("FeelsLikeC", 0)) for h in hourly)
    max_feels = max(int(h.get("FeelsLikeC", 0)) for h in hourly)

    precip = sum(float(h.get("precipMM", 0)) for h in hourly)

    def get_max_precip(hourly_data: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        def max_chance(h: dict[str, Any]) -> int:
            return max(int(h.get("chanceofrain", 0)), int(h.get("chanceofsnow", 0)))

        max_hour = max(hourly_data, key=max_chance)
        return max_chance(max_hour), max_hour

    chance_of_precip, max_precip_hour = get_max_precip(hourly)

    if chance_of_precip >= 35:
        target_hour = max_precip_hour
    else:
        target_hour = hourly[4] if len(hourly) > 4 else hourly[0]

    code_val = int(target_hour.get("weatherCode", 0)) if target_hour.get("weatherCode") else None
    raw_desc = target_hour["weatherDesc"][0]["value"] if target_hour.get("weatherDesc") else ""
    conditions_display = translate_condition(code_val, raw_desc)
    conditions_en = raw_desc.lower()

    tomorrow_chance, tomorrow_max_hour = get_max_precip(tomorrow["hourly"])
    target_tomorrow_hour = tomorrow_max_hour if tomorrow_chance >= 35 else (tomorrow["hourly"][4] if len(tomorrow["hourly"]) > 4 else tomorrow["hourly"][0])
    tmrw_code = int(target_tomorrow_hour.get("weatherCode", 0)) if target_tomorrow_hour.get("weatherCode") else None
    tmrw_raw = target_tomorrow_hour["weatherDesc"][0]["value"] if target_tomorrow_hour.get("weatherDesc") else ""
    tomorrow_cond_display = translate_condition(tmrw_code, tmrw_raw)

    uv_index = int(today.get("uvIndex", 0))
    tomorrow_min = tomorrow.get("mintempC", "-")
    tomorrow_max = tomorrow.get("maxtempC", "-")

    alert_tips: list[str] = []
    is_severe = False
    all_conds_str = " ".join([h["weatherDesc"][0]["value"].lower() for h in hourly])

    if any(k in all_conds_str for k in ("smoke", "haze", "wildfire")):
        alert_tips.append("⚠️ 警报：检测到山火/烟尘霾！空气质量较差，敏感人群外出请佩戴口罩。")
        is_severe = True
    if any(k in all_conds_str for k in ("freezing rain", "ice pellets", "freezing drizzle")):
        alert_tips.append("⚠️ 警报：冻雨天气！道路将极度结冰湿滑（黑冰），极易发生事故，请务必小心出行！")
        is_severe = True
    if any(k in all_conds_str for k in ("thunder", "torrential", "storm")):
        alert_tips.append("⚠️ 警报：强对流/雷暴天气即将来临，注意雷电大风与短时强降水！")
        is_severe = True
    if any(k in all_conds_str for k in ("blizzard", "heavy snow")) or (int(max_precip_hour.get("chanceofsnow", 0)) > 60 and precip > 10.0):
        alert_tips.append("⚠️ 警报：暴雪天气！能见度差且路面积雪严重，请尽量减少非必要外出。")
        is_severe = True

    if min_feels <= -30:
        alert_tips.append(f"⚠️ 极寒警报：最低体感低至 {min_feels}°C！极易发生严重冻伤，尽量避免长时间在户外停留。")
        is_severe = True
    elif min_feels <= -20:
        alert_tips.append(f"🧣 严寒提醒：最低体感低至 {min_feels}°C，风寒刺骨，外出请备齐手套围巾防风保暖。")

    if max_feels >= 38 or max_temp >= 33:
        alert_tips.append(f"🔥 高温警报：最高体感达 {max_feels}°C！注意防暑降温，及时补水，避免正午暴晒。")
        is_severe = True
    elif max_feels >= 32:
        alert_tips.append(f"🌡️ 炎热提醒：最高体感达 {max_feels}°C，午后闷热，外出注意防晒补水。")

    tips: list[str] = []
    is_caution = False

    if chance_of_precip > 50 or precip > 1.5:
        is_caution = True
        if int(max_precip_hour.get("chanceofsnow", 0)) > 50 or "snow" in conditions_en:
            tips.append("❄️ 提醒：今天大概率有降雪，地表湿滑，步行与驾车请注意安全。")
        else:
            tips.append("☔ 提醒：今天大概率有降雨，出门别忘了带伞。")

    if uv_index >= 6:
        is_caution = True
        tips.append(f"🕶️ 提醒：紫外线较强 (UV {uv_index})，外出建议防晒。")

    if max_temp - min_temp >= 12 and min_feels > -20:
        is_caution = True
        tips.append(f"🧥 提醒：昼夜温差达 {max_temp - min_temp}°C，早晚偏凉，注意适时添衣。")
    elif min_temp < 5 and min_feels > -20:
        is_caution = True
        tips.append("🧥 提醒：早晚气温较低，注意添衣保暖。")

    all_tips = alert_tips + tips
    tip_text = "\n".join(all_tips) if all_tips else "🌤️ 今天气温适宜，天气状况良好，祝生活愉快！"

    if is_severe:
        color = COLOR_SEVERE
    elif chance_of_precip > 50 or precip > 1.0:
        if "snow" in conditions_en or int(max_precip_hour.get("chanceofsnow", 0)) > 50:
            color = COLOR_SNOW
        else:
            color = COLOR_RAIN
    elif is_caution:
        color = COLOR_CAUTION
    elif "cloud" in conditions_en or "overcast" in conditions_en:
        color = COLOR_CLOUDY
    else:
        color = COLOR_NICE

    if min_feels == min_temp and max_feels == max_temp:
        temp_val = f"{min_temp}°C ~ {max_temp}°C"
    else:
        temp_val = f"{min_temp}°C ~ {max_temp}°C (体感 {min_feels}°C ~ {max_feels}°C)"

    precip_window = format_precip_window_wttr(hourly)
    if precip_window and (chance_of_precip >= 35 or precip >= 0.4):
        precip_val = f"{chance_of_precip}% (约 {precip:.1f}mm · 时段: {precip_window})"
    else:
        precip_val = f"{chance_of_precip}% (约 {precip:.1f}mm)"

    title_city = f"{display_label} ({search_query})" if display_label != search_query else search_query
    embed = discord.Embed(
        title=f"📍 {title_city} 今日天气",
        color=discord.Color(color),
    )
    embed.add_field(
        name="🌡️ 气温与体感",
        value=temp_val,
        inline=True,
    )
    embed.add_field(
        name="☀️ 天气状况",
        value=f"{conditions_display} (UV指数: {uv_index})",
        inline=True,
    )
    embed.add_field(
        name="💧 降水概率",
        value=precip_val,
        inline=True,
    )
    embed.add_field(
        name="🔮 明日预报",
        value=f"{tomorrow_cond_display}, {tomorrow_min}°C ~ {tomorrow_max}°C",
        inline=False,
    )
    embed.add_field(
        name="💡 出行建议",
        value=tip_text,
        inline=False,
    )
    embed.set_footer(text="数据来源: wttr.in 气象分析")
    return embed


async def fetch_weather_open_meteo(city: str, session: aiohttp.ClientSession, lang: str = "zh") -> discord.Embed:
    search_query, display_label = resolve_city_name(city)
    encoded_city = quote(search_query)
    geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded_city}&count=1&language=en&format=json"
    timeout = aiohttp.ClientTimeout(total=8)

    async with session.get(geo_url, timeout=timeout) as geo_resp:
        geo_resp.raise_for_status()
        geo_data = await geo_resp.json(content_type=None)

    results = geo_data.get("results")
    if not results:
        raise ValueError(f"Open-Meteo 未检索到城市 '{city}'")

    lat = results[0]["latitude"]
    lon = results[0]["longitude"]

    weather_url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        f"&hourly=apparent_temperature,precipitation_probability,weather_code"
        f"&daily=weather_code,temperature_2m_max,temperature_2m_min,uv_index_max,precipitation_probability_max,precipitation_sum"
        f"&timezone=auto&forecast_days=2"
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
    if apparent_temps:
        min_feels = int(min(apparent_temps))
        max_feels = int(max(apparent_temps))
    else:
        min_feels = today_min_temp
        max_feels = today_max_temp

    conditions_display = WMO_CODES.get(today_weather_code, "多云")
    tomorrow_cond_display = WMO_CODES.get(tomorrow_weather_code, "多云")

    alert_tips: list[str] = []
    is_severe = False

    if today_weather_code in [56, 57, 66, 67]:
        alert_tips.append("⚠️ 警报：冻雨天气！道路极度结冰湿滑，行车与步行请格外警惕！")
        is_severe = True
    if today_weather_code in [95, 96, 99]:
        alert_tips.append("⚠️ 警报：雷暴天气即将来临，注意防范雷电大风！")
        is_severe = True
    if today_weather_code in [75, 86]:
        alert_tips.append("⚠️ 警报：暴雪/大雪天气，积雪严重，尽量减少非必要出行！")
        is_severe = True

    if min_feels <= -30:
        alert_tips.append(f"⚠️ 极寒警报：最低体感达 {min_feels}°C！极易冻伤，尽量避免长时间在户外停留。")
        is_severe = True
    elif min_feels <= -20:
        alert_tips.append(f"🧣 严寒提醒：最低体感达 {min_feels}°C，风寒显著，请注意防风防冻保暖。")

    if max_feels >= 38 or today_max_temp >= 33:
        alert_tips.append(f"🔥 高温警报：最高体感达 {max_feels}°C，注意防暑降温，避免正午暴晒。")
        is_severe = True
    elif max_feels >= 32:
        alert_tips.append(f"🌡️ 炎热提醒：最高体感达 {max_feels}°C，午后闷热，外出注意防晒补水。")

    tips: list[str] = []
    is_caution = False
    if today_precip_chance > 50 or today_precip_sum > 1.5:
        is_caution = True
        if today_weather_code in [71, 73, 75, 77, 85, 86]:
            tips.append("❄️ 提醒：今天大概率有雪，路面湿滑，请注意行车安全。")
        else:
            tips.append("☔ 提醒：今天大概率有雨，出门别忘了带伞。")

    if today_uv >= 6:
        is_caution = True
        tips.append(f"🕶️ 提醒：紫外线较强 (UV {today_uv})，外出建议防晒。")

    if today_max_temp - today_min_temp >= 12 and min_feels > -20:
        is_caution = True
        tips.append(f"🧥 提醒：昼夜温差达 {today_max_temp - today_min_temp}°C，早晚注意添衣。")
    elif today_min_temp < 5 and min_feels > -20:
        is_caution = True
        tips.append("🧥 提醒：早晚气温较低，注意保暖。")

    all_tips = alert_tips + tips
    tip_text = "\n".join(all_tips) if all_tips else "🌤️ 今天气温适宜，天气状况良好，祝生活愉快！"

    if is_severe:
        color = COLOR_SEVERE
    elif today_precip_chance > 50 or today_precip_sum > 1.0:
        if today_weather_code in [71, 73, 75, 77, 85, 86]:
            color = COLOR_SNOW
        else:
            color = COLOR_RAIN
    elif is_caution:
        color = COLOR_CAUTION
    elif today_weather_code in [1, 2, 3, 45, 48]:
        color = COLOR_CLOUDY
    else:
        color = COLOR_NICE

    if min_feels == today_min_temp and max_feels == today_max_temp:
        temp_val = f"{today_min_temp}°C ~ {today_max_temp}°C"
    else:
        temp_val = f"{today_min_temp}°C ~ {today_max_temp}°C (体感 {min_feels}°C ~ {max_feels}°C)"

    title_city = f"{display_label} ({search_query})" if display_label != search_query else search_query
    embed = discord.Embed(
        title=f"📍 {title_city} 今日天气",
        color=discord.Color(color),
    )
    embed.add_field(
        name="🌡️ 气温与体感",
        value=temp_val,
        inline=True,
    )
    embed.add_field(
        name="☀️ 天气状况",
        value=f"{conditions_display} (UV指数: {today_uv})",
        inline=True,
    )
    embed.add_field(
        name="💧 降水概率",
        value=f"{today_precip_chance}% (约 {today_precip_sum:.1f}mm)",
        inline=True,
    )
    embed.add_field(
        name="🔮 明日预报",
        value=f"{tomorrow_cond_display}, {tomorrow_min_temp}°C ~ {tomorrow_max_temp}°C",
        inline=False,
    )
    embed.add_field(
        name="💡 出行建议",
        value=tip_text,
        inline=False,
    )
    embed.set_footer(text="数据来源: Open-Meteo (备用通道)")
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
                title=f"📍 {city} 天气获取失败",
                description=f"主通道错误: {primary_error}\n备用通道错误: {fallback_error}",
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
