# Offline Indian city recognition

The public country-wide gate must recognize listings such as `Perundurai` or
`Coimbatore(Ganapathy)` without requiring the literal word `India`.
`app/config/india_cities.txt` contains ASCII names from GeoNames' cities15000
extract, filtered to country code `IN`, plus the source's alternate names
`Ernakulam` and `Kanyakumari`. Common spelling aliases are in `geography.py`.

- Source: https://download.geonames.org/export/dump/cities15000.zip
- Schema and license: https://download.geonames.org/export/dump/readme.txt
- Attribution: GeoNames, https://www.geonames.org/
- License: Creative Commons Attribution 4.0,
  https://creativecommons.org/licenses/by/4.0/
- Retrieved: 2026-09-07
- Input archive SHA-256:
  `dc33eb34a680aaad78b8dcfff9303d9eacc0cc2f7ba640a9c086c34d09eef558`
- Transformation: split the tab-delimited `cities15000.txt`; retain rows whose
  country-code column (index 8) is `IN`; take ASCII name (index 2), add the two
  aliases above verified in alternate names (index 3), deduplicate and sort.

This is location evidence, not a list of allowed searches. Any requested city
is passed to supported sources. The extract is incomplete and names can be
ambiguous. Explicit foreign-country evidence takes precedence; unrecognized
country-less locations remain `needs_review`. No geocoding service or network
request is made during relevance filtering.
