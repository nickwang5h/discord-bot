import datetime
import unittest

from cogs.canada_life import get_canada_holidays


class CanadaLifeTests(unittest.TestCase):
    def test_canada_holidays_has_all_major_dates(self):
        holidays_2026 = get_canada_holidays(2026)
        holiday_names = [name for _, name, _ in holidays_2026]

        self.assertTrue(any("元旦" in name for name in holiday_names))
        self.assertTrue(any("家庭日" in name for name in holiday_names))
        self.assertTrue(any("维多利亚日" in name for name in holiday_names))
        self.assertTrue(any("加拿大国庆日" in name for name in holiday_names))
        self.assertTrue(any("劳动节" in name for name in holiday_names))
        self.assertTrue(any("感恩节" in name for name in holiday_names))
        self.assertTrue(any("圣诞节" in name for name in holiday_names))
        self.assertTrue(any("节礼日" in name for name in holiday_names))

        # Check exact known 2026 dates
        date_map = {name: d for d, name, _ in holidays_2026}
        self.assertEqual(date_map["元旦 (New Year's Day)"], datetime.date(2026, 1, 1))
        self.assertEqual(date_map["加拿大国庆日 (Canada Day)"], datetime.date(2026, 7, 1))
        self.assertEqual(date_map["圣诞节 (Christmas Day)"], datetime.date(2026, 12, 25))
        self.assertEqual(date_map["节礼日 (Boxing Day)"], datetime.date(2026, 12, 26))
        # Labour day 2026 is Sep 7
        self.assertEqual(date_map["劳动节 (Labour Day)"], datetime.date(2026, 9, 7))

    def test_victoria_day_is_the_monday_before_may_25(self):
        # May 25 itself is a Monday in 2026, 2037 and 2043; the holiday is the week before.
        expected = {
            2025: datetime.date(2025, 5, 19),
            2026: datetime.date(2026, 5, 18),
            2027: datetime.date(2027, 5, 24),
            2028: datetime.date(2028, 5, 22),
            2037: datetime.date(2037, 5, 18),
            2043: datetime.date(2043, 5, 18),
        }
        for year, day in expected.items():
            date_map = {name: d for d, name, _ in get_canada_holidays(year)}
            self.assertEqual(date_map["维多利亚日 (Victoria Day)"], day, year)


if __name__ == "__main__":
    unittest.main()
