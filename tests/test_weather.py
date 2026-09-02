import unittest
from unittest.mock import MagicMock

import discord

from core.weather import (
    COLOR_SEVERE,
    build_morning_weather_embeds,
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


if __name__ == "__main__":
    unittest.main()
