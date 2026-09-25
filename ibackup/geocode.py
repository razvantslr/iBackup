"""Offline reverse geocoding: from GPS coordinates to a city name.

Works without internet. By default it uses the short city list in this file; if
you install the GeoNames dataset (`python -m ibackup geonames --install`),
coverage grows to roughly 50,000 places.

The naming rule: among the cities within the configured radius (25 km by
default) we pick the largest one. That way a photo taken in Badalona lands in
"Barcelona" rather than a folder of its own, which is exactly the "within a few
kilometres" grouping we want.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

EARTH_RADIUS_KM = 6371.0088

# Fallback list: approximate city centres, used when the GeoNames dataset is
# not installed. Good enough for cities, not for villages.
BUILTIN_CITIES: list[tuple[str, str, float, float, int]] = [
    # Romania
    ("Bucuresti", "RO", 44.44, 26.10, 1830000), ("Cluj-Napoca", "RO", 46.77, 23.60, 324000),
    ("Timisoara", "RO", 45.75, 21.23, 319000), ("Iasi", "RO", 47.16, 27.59, 290000),
    ("Constanta", "RO", 44.18, 28.65, 283000), ("Craiova", "RO", 44.33, 23.80, 269000),
    ("Brasov", "RO", 45.65, 25.61, 253000), ("Galati", "RO", 45.44, 28.05, 249000),
    ("Ploiesti", "RO", 44.94, 26.03, 209000), ("Oradea", "RO", 47.07, 21.93, 196000),
    ("Braila", "RO", 45.27, 27.97, 180000), ("Arad", "RO", 46.18, 21.31, 159000),
    ("Pitesti", "RO", 44.86, 24.87, 155000), ("Sibiu", "RO", 45.79, 24.15, 147000),
    ("Bacau", "RO", 46.57, 26.91, 144000), ("Targu Mures", "RO", 46.54, 24.56, 134000),
    ("Baia Mare", "RO", 47.66, 23.58, 123000), ("Buzau", "RO", 45.15, 26.82, 115000),
    ("Botosani", "RO", 47.75, 26.67, 106000), ("Satu Mare", "RO", 47.79, 22.88, 102000),
    ("Ramnicu Valcea", "RO", 45.10, 24.37, 98000), ("Suceava", "RO", 47.65, 26.26, 92000),
    ("Drobeta-Turnu Severin", "RO", 44.63, 22.66, 86000), ("Piatra Neamt", "RO", 46.93, 26.37, 85000),
    ("Targu Jiu", "RO", 45.04, 23.27, 82000), ("Targoviste", "RO", 44.93, 25.46, 79000),
    ("Focsani", "RO", 45.70, 27.18, 79000), ("Bistrita", "RO", 47.13, 24.49, 75000),
    ("Resita", "RO", 45.30, 21.89, 73000), ("Tulcea", "RO", 45.18, 28.80, 73000),
    ("Slatina", "RO", 44.43, 24.37, 70000), ("Calarasi", "RO", 44.20, 27.33, 65000),
    ("Alba Iulia", "RO", 46.07, 23.57, 63000), ("Giurgiu", "RO", 43.90, 25.97, 61000),
    ("Deva", "RO", 45.88, 22.90, 61000), ("Hunedoara", "RO", 45.75, 22.90, 60000),
    ("Zalau", "RO", 47.19, 23.06, 56000), ("Sfantu Gheorghe", "RO", 45.87, 25.79, 56000),
    ("Barlad", "RO", 46.23, 27.67, 55000), ("Vaslui", "RO", 46.64, 27.73, 55000),
    ("Roman", "RO", 46.92, 26.93, 50000), ("Turda", "RO", 46.57, 23.79, 47000),
    ("Medias", "RO", 46.16, 24.35, 47000), ("Slobozia", "RO", 44.56, 27.37, 45000),
    ("Alexandria", "RO", 43.97, 25.33, 45000), ("Lugoj", "RO", 45.69, 21.90, 40000),
    ("Onesti", "RO", 46.25, 26.77, 39000), ("Medgidia", "RO", 44.25, 28.27, 39000),
    ("Miercurea Ciuc", "RO", 46.36, 25.80, 38000), ("Sighetu Marmatiei", "RO", 47.93, 23.89, 37000),
    ("Petrosani", "RO", 45.42, 23.37, 37000), ("Mangalia", "RO", 43.82, 28.58, 36000),
    ("Campina", "RO", 45.13, 25.73, 32000), ("Fagaras", "RO", 45.84, 24.97, 30000),
    ("Sighisoara", "RO", 46.22, 24.79, 28000), ("Curtea de Arges", "RO", 45.14, 24.68, 27000),
    ("Vatra Dornei", "RO", 47.35, 25.36, 14000), ("Sinaia", "RO", 45.35, 25.55, 10000),
    ("Busteni", "RO", 45.41, 25.53, 9000), ("Predeal", "RO", 45.50, 25.58, 4700),
    # Europe
    ("London", "GB", 51.51, -0.13, 8900000), ("Paris", "FR", 48.86, 2.35, 2100000),
    ("Madrid", "ES", 40.42, -3.70, 3300000), ("Barcelona", "ES", 41.39, 2.17, 1620000),
    ("Valencia", "ES", 39.47, -0.38, 790000), ("Sevilla", "ES", 37.39, -5.98, 690000),
    ("Malaga", "ES", 36.72, -4.42, 570000), ("Bilbao", "ES", 43.26, -2.93, 345000),
    ("Granada", "ES", 37.18, -3.60, 232000), ("Palma", "ES", 39.57, 2.65, 410000),
    ("Ibiza", "ES", 38.91, 1.43, 50000), ("Lisboa", "PT", 38.72, -9.14, 545000),
    ("Porto", "PT", 41.15, -8.61, 232000), ("Roma", "IT", 41.90, 12.50, 2870000),
    ("Milano", "IT", 45.46, 9.19, 1370000), ("Napoli", "IT", 40.85, 14.27, 960000),
    ("Torino", "IT", 45.07, 7.69, 870000), ("Venezia", "IT", 45.44, 12.32, 260000),
    ("Firenze", "IT", 43.77, 11.26, 380000), ("Bologna", "IT", 44.49, 11.34, 390000),
    ("Genova", "IT", 44.41, 8.93, 580000), ("Verona", "IT", 45.44, 10.99, 257000),
    ("Pisa", "IT", 43.72, 10.40, 91000), ("Palermo", "IT", 38.12, 13.36, 660000),
    ("Catania", "IT", 37.51, 15.08, 311000), ("Bari", "IT", 41.12, 16.87, 320000),
    ("Berlin", "DE", 52.52, 13.40, 3670000), ("Hamburg", "DE", 53.55, 9.99, 1840000),
    ("Munchen", "DE", 48.14, 11.58, 1480000), ("Koln", "DE", 50.94, 6.96, 1080000),
    ("Frankfurt", "DE", 50.11, 8.68, 760000), ("Wien", "AT", 48.21, 16.37, 1900000),
    ("Salzburg", "AT", 47.81, 13.05, 155000), ("Innsbruck", "AT", 47.27, 11.39, 132000),
    ("Graz", "AT", 47.07, 15.44, 290000), ("Praha", "CZ", 50.08, 14.44, 1300000),
    ("Budapest", "HU", 47.50, 19.04, 1750000), ("Warszawa", "PL", 52.23, 21.01, 1790000),
    ("Krakow", "PL", 50.06, 19.94, 780000), ("Amsterdam", "NL", 52.37, 4.90, 870000),
    ("Rotterdam", "NL", 51.92, 4.48, 650000), ("Brussel", "BE", 50.85, 4.35, 1200000),
    ("Antwerpen", "BE", 51.22, 4.40, 530000), ("Brugge", "BE", 51.21, 3.22, 118000),
    ("Zurich", "CH", 47.37, 8.54, 420000), ("Geneve", "CH", 46.20, 6.14, 200000),
    ("Bern", "CH", 46.95, 7.45, 134000), ("Basel", "CH", 47.56, 7.59, 175000),
    ("Luzern", "CH", 47.05, 8.31, 82000), ("Interlaken", "CH", 46.69, 7.86, 5600),
    ("Zermatt", "CH", 46.02, 7.75, 5800), ("Luxembourg", "LU", 49.61, 6.13, 125000),
    ("Kobenhavn", "DK", 55.68, 12.57, 640000), ("Stockholm", "SE", 59.33, 18.07, 975000),
    ("Goteborg", "SE", 57.71, 11.97, 580000), ("Oslo", "NO", 59.91, 10.75, 700000),
    ("Bergen", "NO", 60.39, 5.32, 280000), ("Tromso", "NO", 69.65, 18.96, 76000),
    ("Helsinki", "FI", 60.17, 24.94, 650000), ("Reykjavik", "IS", 64.15, -21.94, 130000),
    ("Dublin", "IE", 53.35, -6.26, 590000), ("Edinburgh", "GB", 55.95, -3.19, 525000),
    ("Manchester", "GB", 53.48, -2.24, 553000), ("Nice", "FR", 43.70, 7.27, 342000),
    ("Marseille", "FR", 43.30, 5.37, 870000), ("Lyon", "FR", 45.76, 4.84, 520000),
    ("Bordeaux", "FR", 44.84, -0.58, 260000), ("Toulouse", "FR", 43.60, 1.44, 490000),
    ("Strasbourg", "FR", 48.57, 7.75, 285000), ("Cannes", "FR", 43.55, 7.02, 74000),
    ("Monaco", "MC", 43.73, 7.42, 39000), ("Athina", "GR", 37.98, 23.73, 665000),
    ("Thessaloniki", "GR", 40.64, 22.94, 325000), ("Santorini", "GR", 36.42, 25.43, 15000),
    ("Istanbul", "TR", 41.01, 28.98, 15500000), ("Antalya", "TR", 36.90, 30.69, 1200000),
    ("Sofia", "BG", 42.70, 23.32, 1240000), ("Beograd", "RS", 44.79, 20.45, 1370000),
    ("Zagreb", "HR", 45.81, 15.98, 790000), ("Split", "HR", 43.51, 16.44, 160000),
    ("Dubrovnik", "HR", 42.65, 18.09, 42000), ("Ljubljana", "SI", 46.06, 14.51, 295000),
    ("Bratislava", "SK", 48.15, 17.11, 430000), ("Chisinau", "MD", 47.01, 28.86, 640000),
    ("Kyiv", "UA", 50.45, 30.52, 2960000), ("Moskva", "RU", 55.76, 37.62, 12500000),
    ("Sankt-Peterburg", "RU", 59.94, 30.31, 5400000), ("Riga", "LV", 56.95, 24.11, 615000),
    ("Vilnius", "LT", 54.69, 25.28, 580000), ("Tallinn", "EE", 59.44, 24.75, 440000),
    # Americas
    ("New York", "US", 40.71, -74.01, 8300000), ("Los Angeles", "US", 34.05, -118.24, 3900000),
    ("San Francisco", "US", 37.77, -122.42, 875000), ("Chicago", "US", 41.88, -87.63, 2700000),
    ("Seattle", "US", 47.61, -122.33, 740000), ("Boston", "US", 42.36, -71.06, 690000),
    ("Washington", "US", 38.91, -77.04, 700000), ("Miami", "US", 25.77, -80.19, 450000),
    ("Las Vegas", "US", 36.17, -115.14, 650000), ("Denver", "US", 39.74, -104.99, 715000),
    ("Austin", "US", 30.27, -97.74, 960000), ("Honolulu", "US", 21.31, -157.86, 350000),
    ("Toronto", "CA", 43.65, -79.38, 2790000), ("Vancouver", "CA", 49.28, -123.12, 660000),
    ("Montreal", "CA", 45.50, -73.57, 1780000), ("Ciudad de Mexico", "MX", 19.43, -99.13, 9200000),
    ("Cancun", "MX", 21.16, -86.85, 890000), ("La Habana", "CU", 23.11, -82.37, 2100000),
    ("Rio de Janeiro", "BR", -22.91, -43.17, 6700000), ("Sao Paulo", "BR", -23.55, -46.63, 12300000),
    ("Buenos Aires", "AR", -34.60, -58.38, 3100000), ("Lima", "PE", -12.05, -77.04, 9700000),
    ("Santiago", "CL", -33.45, -70.67, 5600000), ("Bogota", "CO", 4.71, -74.07, 7400000),
    # Africa & Middle East
    ("Dubai", "AE", 25.20, 55.27, 3400000), ("Abu Dhabi", "AE", 24.45, 54.38, 1500000),
    ("Doha", "QA", 25.29, 51.53, 1200000), ("Tel Aviv", "IL", 32.08, 34.78, 460000),
    ("Yerushalayim", "IL", 31.77, 35.21, 940000), ("Cairo", "EG", 30.04, 31.24, 9500000),
    ("Marrakech", "MA", 31.63, -7.99, 930000), ("Casablanca", "MA", 33.57, -7.59, 3400000),
    ("Cape Town", "ZA", -33.92, 18.42, 4600000), ("Johannesburg", "ZA", -26.20, 28.05, 5600000),
    ("Nairobi", "KE", -1.29, 36.82, 4400000), ("Zanzibar", "TZ", -6.17, 39.20, 220000),
    # Asia & Oceania
    ("Tokyo", "JP", 35.68, 139.69, 13900000), ("Osaka", "JP", 34.69, 135.50, 2700000),
    ("Kyoto", "JP", 35.01, 135.77, 1460000), ("Seoul", "KR", 37.57, 126.98, 9700000),
    ("Beijing", "CN", 39.90, 116.41, 21500000), ("Shanghai", "CN", 31.23, 121.47, 24900000),
    ("Hong Kong", "HK", 22.32, 114.17, 7500000), ("Singapore", "SG", 1.35, 103.82, 5700000),
    ("Bangkok", "TH", 13.76, 100.50, 8300000), ("Phuket", "TH", 7.88, 98.39, 80000),
    ("Kuala Lumpur", "MY", 3.14, 101.69, 1800000), ("Denpasar", "ID", -8.65, 115.22, 900000),
    ("Jakarta", "ID", -6.21, 106.85, 10600000), ("Manila", "PH", 14.60, 120.98, 1800000),
    ("Delhi", "IN", 28.61, 77.21, 16800000), ("Mumbai", "IN", 19.08, 72.88, 12400000),
    ("Panaji", "IN", 15.50, 73.83, 114000), ("Kathmandu", "NP", 27.72, 85.32, 1000000),
    ("Colombo", "LK", 6.93, 79.86, 750000), ("Male", "MV", 4.18, 73.51, 150000),
    ("Sydney", "AU", -33.87, 151.21, 5300000), ("Melbourne", "AU", -37.81, 144.96, 5000000),
    ("Brisbane", "AU", -27.47, 153.03, 2500000), ("Auckland", "NZ", -36.85, 174.76, 1650000),
    ("Queenstown", "NZ", -45.03, 168.66, 16000),
]


@dataclass(frozen=True)
class Place:
    name: str
    country: str
    distance_km: float
    source: str  # 'geonames' | 'builtin' | 'grid'


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def grid_label(lat: float, lon: float) -> str:
    """Fallback label when no city is close enough: GPS_44.4N_26.1E."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"GPS_{abs(lat):.1f}{ns}_{abs(lon):.1f}{ew}"


def load_geonames(path) -> list[tuple[str, str, float, float, int]]:
    """Reads either a GeoNames dump (cities5000.txt) or our compact TSV."""
    cities: list[tuple[str, str, float, float, int]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            try:
                if len(cols) >= 15:  # GeoNames dump
                    name = cols[2].strip() or cols[1].strip()
                    lat, lon = float(cols[4]), float(cols[5])
                    country = cols[8].strip()
                    population = int(cols[14] or 0)
                elif len(cols) >= 4:  # name / country / lat / lon [/ population]
                    name = cols[0].strip()
                    country = cols[1].strip()
                    lat, lon = float(cols[2]), float(cols[3])
                    population = int(cols[4]) if len(cols) > 4 and cols[4] else 0
                else:
                    continue
            except ValueError:
                continue
            if name:
                cities.append((name, country, lat, lon, population))
    return cities


class Geocoder:
    """Finds the best-fitting city for a pair of coordinates."""

    def __init__(self, cities=None, *, radius_km: float = 25.0,
                 prefer: str = "population", cluster_decimals: int = 2,
                 aliases: dict[str, str] | None = None, source: str = "builtin"):
        self.radius_km = radius_km
        self.prefer = prefer
        self.cluster_decimals = cluster_decimals
        self.aliases = {k.lower(): v for k, v in (aliases or {}).items()}
        self.source = source
        self.cities = list(cities if cities is not None else BUILTIN_CITIES)
        self._grid: dict[tuple[int, int], list[int]] = {}
        for index, (_, _, lat, lon, _) in enumerate(self.cities):
            self._grid.setdefault((math.floor(lat), math.floor(lon)), []).append(index)
        self._cache: dict[tuple[float, float], Place | None] = {}

    @classmethod
    def from_dataset(cls, path, **kwargs) -> "Geocoder":
        cities = load_geonames(path)
        if not cities:
            raise ValueError(f"Dataset {path} contains no cities")
        kwargs.setdefault("source", "geonames")
        return cls(cities, **kwargs)

    def cluster_key(self, lat: float, lon: float) -> tuple[float, float]:
        """Round coordinates so photos from one spot resolve to one city."""
        d = self.cluster_decimals
        return (round(lat, d), round(lon, d))

    def _nearby_indexes(self, lat: float, lon: float, radius_km: float):
        dlat = radius_km / 111.0
        cos_lat = max(math.cos(math.radians(lat)), 0.01)
        dlon = radius_km / (111.0 * cos_lat)
        lat_cells = range(math.floor(lat - dlat), math.floor(lat + dlat) + 1)
        lon_span = math.floor(lon + dlon) - math.floor(lon - dlon)
        lon_start = math.floor(lon - dlon)
        for ilat in lat_cells:
            for step in range(lon_span + 1):
                ilon = lon_start + step
                # Wrap longitude so the search also works across the 180th meridian.
                if ilon > 180:
                    ilon -= 360
                elif ilon < -180:
                    ilon += 360
                for index in self._grid.get((ilat, ilon), ()):
                    yield index

    def lookup(self, lat: float, lon: float) -> Place | None:
        key = self.cluster_key(lat, lon)
        if key in self._cache:
            return self._cache[key]

        best: Place | None = None
        best_score: tuple[int, float] | None = None
        for index in self._nearby_indexes(key[0], key[1], self.radius_km):
            name, country, clat, clon, population = self.cities[index]
            distance = haversine_km(key[0], key[1], clat, clon)
            if distance > self.radius_km:
                continue
            if self.prefer == "nearest":
                score = (0, -distance)
            else:
                score = (population, -distance)
            if best_score is None or score > best_score:
                best_score = score
                best = Place(name=name, country=country, distance_km=distance,
                             source=self.source)
        self._cache[key] = best
        return best

    def place_name(self, lat: float | None, lon: float | None) -> tuple[str, str]:
        """Returns (folder_name, source). Never returns an empty name."""
        if lat is None or lon is None:
            return "", ""
        place = self.lookup(lat, lon)
        if place is None:
            return grid_label(*self.cluster_key(lat, lon)), "grid"
        name = self.aliases.get(place.name.lower(), place.name)
        return name, place.source
