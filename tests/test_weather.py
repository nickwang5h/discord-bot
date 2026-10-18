import datetime as dt
import unittest
from unittest.mock import MagicMock

import discord

from core.weather import (
    COLOR_RAIN,
    COLOR_SEVERE,
    CityNotFoundError,
    ForecastRangeError,
    build_morning_weather_embeds,
    fetch_forecast_range,
    fetch_weather_wttr,
    get_weather_embed,
    resolve_city_name,
)


class MockResponse:
    def __init__(self, status: int, data: dict):
        self.status = status
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self, content_type=None):
        return self._data


class WeatherTests(unittest.IsolatedAsyncioTestCase):
    def _sample_wttr_payload(self, desc="晴朗", precip_mm="0.0", chance_rain="10", code="113", feels="12"):
        return {
            "weather": [
                {
                    "mintempC": "12",
                    "maxtempC": "24",
                    "uvIndex": "3",
                    "hourly": [
                        {
                            "time": "0",
                            "FeelsLikeC": str(feels),
                            "precipMM": "0.0",
                            "chanceofrain": "0",
                            "chanceofsnow": "0",
                            "weatherCode": code,
                            "weatherDesc": [{"value": desc}],
                        },
                        {
                            "time": "1200",
                            "FeelsLikeC": "25",
                            "precipMM": str(precip_mm),
                            "chanceofrain": str(chance_rain),
                            "chanceofsnow": "0",
                            "weatherCode": code,
                            "weatherDesc": [{"value": desc}],
                        },
                        {
                            "time": "1500",
                            "FeelsLikeC": "24",
                            "precipMM": "0.0",
                            "chanceofrain": "0",
                            "chanceofsnow": "0",
                            "weatherCode": code,
                            "weatherDesc": [{"value": desc}],
                        },
                        {
                            "time": "1800",
                            "FeelsLikeC": "20",
                            "precipMM": "0.0",
                            "chanceofrain": "0",
                            "chanceofsnow": "0",
                            "weatherCode": code,
                            "weatherDesc": [{"value": desc}],
                        },
                        {
                            "time": "2100",
                            "FeelsLikeC": "16",
                            "precipMM": "0.0",
                            "chanceofrain": "0",
                            "chanceofsnow": "0",
                            "weatherCode": code,
                            "weatherDesc": [{"value": desc}],
                        },
                    ],
                },
                {
                    "mintempC": "14",
                    "maxtempC": "26",
                    "hourly": [
                        {
                            "weatherCode": "113",
                            "weatherDesc": [{"value": "Clear"}],
                            "chanceofrain": "0",
                            "chanceofsnow": "0",
                        }
                    ] * 5,
                },
            ]
        }

    def test_city_alias_resolution(self):
        self.assertEqual(resolve_city_name("渥太华"), ("Ottawa", "渥太华"))
        self.assertEqual(resolve_city_name("萨德伯里"), ("Sudbury", "萨德伯里"))
        self.assertEqual(resolve_city_name("Toronto"), ("Toronto", "Toronto"))

    async def test_wttr_success(self):
        payload = self._sample_wttr_payload()
        session = MagicMock()
        session.get.return_value = MockResponse(200, payload)

        embed = await fetch_weather_wttr("Ottawa", session, lang="zh")
        self.assertIsInstance(embed, discord.Embed)
        self.assertEqual(embed.title, "📍 Ottawa 今日天气")
        self.assertIn("wttr.in", embed.footer.text)

        field_names = [f.name for f in embed.fields]
        self.assertIn("🌡️ 气温与体感", field_names)
        self.assertIn("☀️ 天气状况", field_names)
        self.assertIn("💧 降水概率", field_names)
        self.assertIn("🔮 明日预报", field_names)
        self.assertIn("💡 出行建议", field_names)

        temp_field = next(f for f in embed.fields if f.name == "🌡️ 气温与体感")
        self.assertIn("12°C ~ 24°C", temp_field.value)

    async def test_open_meteo_fallback_on_wttr_failure(self):
        geo_payload = {
            "results": [{"latitude": 45.42, "longitude": -75.69}]
        }
        forecast_payload = {
            "hourly": {
                "apparent_temperature": [20] * 24,
            },
            "daily": {
                "temperature_2m_max": [25, 27],
                "temperature_2m_min": [15, 17],
                "uv_index_max": [4, 5],
                "precipitation_probability_max": [20, 10],
                "precipitation_sum": [0.0, 0.0],
                "weather_code": [0, 1],
            },
        }

        def fake_get(url, **kwargs):
            if "wttr.in" in url:
                return MockResponse(500, {})
            if "geocoding-api" in url:
                return MockResponse(200, geo_payload)
            if "api.open-meteo.com" in url:
                return MockResponse(200, forecast_payload)
            return MockResponse(404, {})

        session = MagicMock()
        session.get.side_effect = fake_get

        embed = await get_weather_embed("Ottawa", session, lang="zh")
        self.assertIsInstance(embed, discord.Embed)
        self.assertEqual(embed.title, "📍 Ottawa 今日天气")
        self.assertIn("Open-Meteo", embed.footer.text)

    async def test_both_apis_fail_returns_error_embed(self):
        session = MagicMock()
        session.get.return_value = MockResponse(503, {})

        embed = await get_weather_embed("UnknownCity", session, lang="zh")
        self.assertIsInstance(embed, discord.Embed)
        self.assertIn("失败", embed.title)
        self.assertEqual(embed.color.value, COLOR_SEVERE)

    async def test_severe_thunderstorm_alert(self):
        payload = self._sample_wttr_payload(desc="Heavy Thunderstorm", precip_mm="15.0", chance_rain="90", code="389")
        session = MagicMock()
        session.get.return_value = MockResponse(200, payload)

        embed = await fetch_weather_wttr("Ottawa", session, lang="zh")
        self.assertEqual(embed.color.value, COLOR_SEVERE)
        tips_field = next(f for f in embed.fields if f.name == "💡 出行建议")
        self.assertIn("雷暴", tips_field.value)

    async def test_extreme_cold_alert(self):
        payload = self._sample_wttr_payload(desc="Clear", precip_mm="0.0", chance_rain="0", code="113", feels="-32")
        session = MagicMock()
        session.get.return_value = MockResponse(200, payload)

        embed = await fetch_weather_wttr("Ottawa", session, lang="zh")
        self.assertEqual(embed.color.value, COLOR_SEVERE)
        tips_field = next(f for f in embed.fields if f.name == "💡 出行建议")
        self.assertIn("极寒警报", tips_field.value)
        self.assertIn("-32°C", tips_field.value)

    async def test_build_morning_weather_embeds(self):
        payload = self._sample_wttr_payload()
        session = MagicMock()
        session.get.return_value = MockResponse(200, payload)

        embeds = await build_morning_weather_embeds(["Ottawa", "Sudbury"], session, lang="zh")
        self.assertEqual(len(embeds), 2)
        self.assertEqual(embeds[0].title, "📍 Ottawa 今日天气")
        self.assertEqual(embeds[1].title, "📍 Sudbury 今日天气")


TODAY = dt.date(2026, 10, 18)


def _range_payload(start=TODAY, days=5, **overrides):
    daily = {
        "time": [(start + dt.timedelta(days=i)).isoformat() for i in range(days)],
        "temperature_2m_max": [12.4 + i for i in range(days)],
        "temperature_2m_min": [3.6 + i for i in range(days)],
        "apparent_temperature_max": [10.0 + i for i in range(days)],
        "apparent_temperature_min": [0.2 + i for i in range(days)],
        "precipitation_probability_max": [10] * days,
        "precipitation_sum": [0.0] * days,
        "weathercode": [1] * days,
        "windspeed_10m_max": [15.0] * days,
        "uv_index_max": [2.0] * days,
    }
    daily.update(overrides)
    return {"daily": daily}


class ForecastRangeTests(unittest.IsolatedAsyncioTestCase):
    def _session(self, forecast_payload, geo_results=None):
        geo = {"results": [{"latitude": 43.65, "longitude": -79.38}]} if geo_results is None else geo_results
        session = MagicMock()
        urls: list[str] = []

        def fake_get(url, **kwargs):
            urls.append(url)
            if "geocoding-api" in url:
                return MockResponse(200, geo)
            if "api.open-meteo.com" in url:
                return MockResponse(200, forecast_payload)
            raise AssertionError(f"unexpected URL {url}")

        session.get.side_effect = fake_get
        return session, urls

    async def test_multi_day_render_slices_requested_dates(self):
        payload = _range_payload(
            precipitation_probability_max=[10, 80, 20, 10, 10],
            precipitation_sum=[0.0, 6.2, 0.0, 0.0, 0.0],
            weathercode=[1, 61, 3, 0, 0],
            windspeed_10m_max=[15.0, 45.2, 20.0, 10.0, 10.0],
        )
        session, urls = self._session(payload)

        embed = await fetch_forecast_range(
            "多伦多", dt.date(2026, 10, 19), dt.date(2026, 10, 21), session,
            today=TODAY, preface="❓ 提问: 多伦多19-21日天气",
        )

        self.assertEqual(embed.title, "📍 多伦多 (Toronto) 10/19–10/21 天气预报")
        self.assertEqual(embed.footer.text, "数据来源: Open-Meteo")
        self.assertTrue(embed.description.startswith("❓ 提问: 多伦多19-21日天气"))
        self.assertIn("**10/19 周一** · 小雨 · 5~13°C (体感 1~11°C) · 降水 80% / 6.2mm · 风 45 km/h", embed.description)
        self.assertIn("10/21 周三", embed.description)
        self.assertNotIn("10/18", embed.description)
        self.assertNotIn("10/22", embed.description)
        tips = embed.fields[0].value
        self.assertIn("☔ 10/19", tips)
        self.assertIn("💨 10/19", tips)
        self.assertIn("最低体感 1°C", tips)
        self.assertEqual(embed.color.value, COLOR_RAIN)
        forecast_url = next(u for u in urls if "/v1/forecast" in u)
        self.assertIn("forecast_days=5", forecast_url)
        self.assertIn("timezone=auto", forecast_url)
        self.assertIn("windspeed_10m_max", forecast_url)
        self.assertIn("apparent_temperature_min", forecast_url)

    async def test_single_day_title_and_calm_tip(self):
        payload = _range_payload(
            temperature_2m_min=[15] * 5, apparent_temperature_min=[15] * 5,
            temperature_2m_max=[22] * 5, apparent_temperature_max=[22] * 5,
        )
        session, _ = self._session(payload)

        embed = await fetch_forecast_range("Ottawa", dt.date(2026, 10, 19), dt.date(2026, 10, 19), session, today=TODAY)

        self.assertEqual(embed.title, "📍 Ottawa 10/19 天气预报")
        self.assertEqual(embed.description.count("**"), 2)
        self.assertIn("期间天气平稳", embed.fields[0].value)

    async def test_range_beyond_16_days_is_rejected_without_http(self):
        session = MagicMock()
        session.get.side_effect = AssertionError("no HTTP for invalid range")

        with self.assertRaises(ForecastRangeError):
            await fetch_forecast_range("Ottawa", TODAY, TODAY + dt.timedelta(days=16), session, today=TODAY)
        with self.assertRaises(ForecastRangeError):
            await fetch_forecast_range("Ottawa", TODAY + dt.timedelta(days=2), TODAY, session, today=TODAY)
        with self.assertRaises(ForecastRangeError):
            await fetch_forecast_range("Ottawa", TODAY - dt.timedelta(days=1), TODAY, session, today=TODAY)

    async def test_full_16_day_range_is_allowed(self):
        session, urls = self._session(_range_payload(days=16))

        embed = await fetch_forecast_range("Ottawa", TODAY, TODAY + dt.timedelta(days=15), session, today=TODAY)

        self.assertEqual(embed.description.count("**") // 2, 16)
        self.assertIn("forecast_days=16", next(u for u in urls if "/v1/forecast" in u))

    async def test_unknown_city_raises_city_not_found(self):
        session, _ = self._session({}, geo_results={})

        with self.assertRaises(CityNotFoundError):
            await fetch_forecast_range("Nowhereville", TODAY, TODAY, session, today=TODAY)

    async def test_long_description_truncates_by_day(self):
        session, _ = self._session(_range_payload(days=16))

        embed = await fetch_forecast_range(
            "Ottawa", TODAY, TODAY + dt.timedelta(days=15), session,
            today=TODAY, preface="❓ 提问: " + "长" * 3000,
        )

        self.assertLessEqual(len(embed.description), 4096)
        self.assertIn("天因 Discord 长度限制已截断", embed.description)


if __name__ == "__main__":
    unittest.main()
