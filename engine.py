"""
Natal chart (D1, sidereal Lahiri, whole-sign houses from the ascendant) — only what
/first-map needs: ascendant sign, planet signs/houses and the ascendant lord.

The full pre-Z3 engine (Tithi Pravesha, hora, dashas, …) lives in research/decision_tool/engine.py.
"""

from datetime import datetime

import pytz
import swisseph as swe
from timezonefinder import TimezoneFinder


class ChartData:
    def __init__(self, date: str, real_time: str, latitude: float, longitude: float):
        self.date = date
        self.real_time = real_time
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.timezone = TimezoneFinder().timezone_at(lat=self.latitude, lng=self.longitude) or "UTC"

        self.PLANETS = {
            'SU': swe.SUN,
            'MO': swe.MOON,
            'ME': swe.MERCURY,
            'VE': swe.VENUS,
            'MA': swe.MARS,
            'JU': swe.JUPITER,
            'SA': swe.SATURN,
        }

        self.PLANET_NAMES = {
            'RA': 'Rahu',
            'KE': 'Ketu',
            'SU': 'Słońce',
            'MO': 'Księżyc',
            'ME': 'Merkury',
            'VE': 'Wenus',
            'MA': 'Mars',
            'JU': 'Jowisz',
            'SA': 'Saturn'
        }

        self.ZODIAC_SIGNS = [
            "Baran", "Byk", "Bliźnięta", "Rak",
            "Lew", "Panna", "Waga", "Skorpion",
            "Strzelec", "Koziorożec", "Wodnik", "Ryby"
        ]

        self.AS_LORD = {
            "Baran": 'MA',
            "Byk": 'VE',
            "Bliźnięta": 'ME',
            "Rak": 'MO',
            "Lew": 'SU',
            "Panna": 'ME',
            "Waga": 'VE',
            "Skorpion": 'MA',
            "Strzelec": 'JU',
            "Koziorożec": 'SA',
            "Wodnik": 'SA',
            "Ryby": 'JU'
        }

        self.birth_time_for_calc()
        self.get_chart_data()

    def birth_time_for_calc(self):
        local = pytz.timezone(self.timezone)
        local_time = datetime.strptime(f"{self.date} {self.real_time}", "%Y-%m-%d %H:%M:%S")
        utc_dt = local.localize(local_time).astimezone(pytz.utc)

        self.utc_date = utc_dt.strftime("%Y-%m-%d")
        self.calc_time = utc_dt.strftime("%H:%M:%S")

    def get_complete_astro_details(self):
        """Sidereal positions → {code: {'house', 'zodiac_sign'}} for AS, RA, KE and the seven planets."""
        self.detailed_chart_data = {}
        swe.set_sid_mode(swe.SIDM_LAHIRI)

        year, month, day = map(int, self.utc_date.split('-'))
        hour, minute, second = map(int, self.calc_time.split(':'))
        decimal_hour = hour + (minute + second / 60.0) / 60.0
        julian_day = swe.julday(year, month, day, decimal_hour)

        ayanamsa = swe.get_ayanamsa_ut(julian_day)

        # Only the ascendant is needed, and ascmc[0] is the same in every house system. Whole
        # sign works at any latitude; Placidus fails beyond the polar circles (~66°).
        houses = swe.houses(julian_day, self.latitude, self.longitude, b'W')
        ascendant_position = (houses[1][0] - ayanamsa) % 360
        self.ascendant_zodiac_index = int(ascendant_position // 30)
        self.detailed_chart_data['AS'] = {
            'house': 1,
            'zodiac_sign': self.ZODIAC_SIGNS[self.ascendant_zodiac_index],
        }

        # Rahu (true node) and Ketu opposite; rounded to 2 decimals as in the original engine.
        rahu_position = round((swe.calc_ut(julian_day, swe.TRUE_NODE, swe.FLG_SWIEPH)[0][0] - ayanamsa) % 360, 2)
        ketu_position = round((rahu_position + 180) % 360, 2)
        for code, position in (('RA', rahu_position), ('KE', ketu_position)):
            self.detailed_chart_data[code] = self._sign_and_house(position)

        for planet_name, planet_code in self.PLANETS.items():
            planet_position = swe.calc_ut(julian_day, planet_code, swe.FLG_SWIEPH)[0][0]
            self.detailed_chart_data[planet_name] = self._sign_and_house((planet_position - ayanamsa) % 360)

    def _sign_and_house(self, position: float) -> dict:
        zodiac_index = int(position // 30)
        return {
            'house': (zodiac_index - self.ascendant_zodiac_index) % 12 + 1,
            'zodiac_sign': self.ZODIAC_SIGNS[zodiac_index],
        }

    def get_chart_data(self):
        self.get_complete_astro_details()
        self.chart_data = {}
        for key, value in self.detailed_chart_data.items():
            self.chart_data[key] = [value['house'], value['zodiac_sign']]
        return self.detailed_chart_data, self.chart_data

    def get_ascendent_and_its_lord_signs(self):
        """
        Tworzy słownik, który mapuje znak zodiaku ascendentu na znak zodiaku, w którym znajduje się jego władca.
        """
        ascendent_sign = self.chart_data['AS'][1]
        as_lord_planet = self.AS_LORD.get(ascendent_sign)
        if not as_lord_planet:
            raise ValueError(f"Nie znaleziono władcy dla znaku zodiaku {ascendent_sign}.")
        lord_planet_sign = None
        for planet, data in self.chart_data.items():
            if planet == as_lord_planet:
                lord_planet_sign = data[1]
                break
        if not lord_planet_sign:
            raise ValueError(f"Nie znaleziono pozycji planety {as_lord_planet} w horoskopie.")
        self.ascendent_and_its_lord_signs = {ascendent_sign: lord_planet_sign}
        return self.ascendent_and_its_lord_signs
