"""Indian location evidence and common city aliases; not a query allowlist.

Arbitrary user-supplied cities remain valid search targets. This vocabulary
recognizes country-less locations in broad feeds; unrecognized locations
still require country evidence or human review.
"""
from functools import lru_cache
from pathlib import Path
import re
import unicodedata

INDIA_CITIES = (
    "Bengaluru", "Bangalore", "Hyderabad", "Pune", "Noida", "Gurugram",
    "Gurgaon", "Chennai", "Mumbai", "Kolkata", "Delhi", "Surat",
    "Vadodara", "Bhubaneswar", "Trivandrum", "Thiruvananthapuram", "Kochi",
    "Ahmedabad", "Madurai", "Indore", "Jaipur", "Jodhpur", "Navi Mumbai",
    "Belagavi",
)

CITY_ALIASES = (
    ("Bengaluru", "Bangalore"),
    ("Gurugram", "Gurgaon"),
    ("Mumbai", "Bombay"),
    ("Chennai", "Madras"),
    ("Kolkata", "Calcutta"),
    ("Thiruvananthapuram", "Trivandrum"),
    ("Kochi", "Cochin"),
    ("Vadodara", "Baroda"),
    ("Belagavi", "Belgaum"),
)


def city_aliases(city: str) -> tuple[str, ...]:
    return next(
        (aliases for aliases in CITY_ALIASES if city.casefold() in
         {alias.casefold() for alias in aliases}),
        (city,),
    )


def _words(text: str) -> tuple[str, ...]:
    text = "".join(char for char in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(char))
    return tuple(re.findall(r"[^\W_]+", text.casefold()))


@lru_cache(maxsize=1)
def _indian_city_names() -> tuple[frozenset[tuple[str, ...]], int]:
    lines = Path(__file__).with_name("india_cities.txt").read_text(encoding="utf-8").splitlines()
    names = {_words(line) for line in lines if line and not line.startswith("#")}
    names.update(_words(alias) for group in CITY_ALIASES for alias in group)
    return frozenset(names), max(map(len, names))


def has_indian_city(text: str) -> bool:
    """Recognize location evidence offline; never restrict requested city names."""
    words = _words(text)
    names, max_words = _indian_city_names()
    return any(words[start:start + length] in names
               for start in range(len(words))
               for length in range(1, min(max_words, len(words) - start) + 1))
