"""
check_countries.py
------------------
Audits all country names stored in organized_trials against the GeoJSON
feature names in echarts-countries-js world.js.

Run this whenever new countries appear in the DB to check if WorldMap.tsx
needs a new NAME_MAP entry.

Usage:
    python check_countries.py
"""

import db

# Names extracted from echarts-countries-js@1.0.5 world.js
GEOJSON_NAMES = {
    "Afghanistan","Aland","Albania","Algeria","American Samoa","Andorra","Angola",
    "Antigua and Barb.","Argentina","Armenia","Australia","Austria","Azerbaijan",
    "Bahamas","Bahrain","Bangladesh","Barbados","Belarus","Belgium","Belize","Benin",
    "Bermuda","Bhutan","Bolivia","Bosnia and Herz.","Botswana","Br. Indian Ocean Ter.",
    "Brazil","Brunei","Bulgaria","Burkina Faso","Burundi","Cambodia","Cameroon",
    "Canada","Cape Verde","Cayman Is.","Central African Rep.","Chad","Chile","China",
    "Colombia","Comoros","Congo","Costa Rica","Côte d'Ivoire","Croatia","Cuba",
    "Curaçao","Cyprus","Czech Rep.","Dem. Rep. Congo","Dem. Rep. Korea","Denmark",
    "Djibouti","Dominica","Dominican Rep.","Ecuador","Egypt","El Salvador",
    "Eq. Guinea","Eritrea","Estonia","Ethiopia","Faeroe Is.","Falkland Is.","Fiji",
    "Finland","Fr. Polynesia","Fr. S. Antarctic Lands","France","Gabon","Gambia",
    "Georgia","Germany","Ghana","Greece","Greenland","Grenada","Guam","Guatemala",
    "Guinea","Guinea-Bissau","Guyana","Haiti","Heard I. and McDonald Is.","Honduras",
    "Hungary","Iceland","India","Indonesia","Iran","Iraq","Ireland","Isle of Man",
    "Israel","Italy","Jamaica","Japan","Jersey","Jordan","Kazakhstan","Kenya",
    "Kiribati","Korea","Kuwait","Kyrgyzstan","Lao PDR","Latvia","Lebanon","Lesotho",
    "Liberia","Libya","Liechtenstein","Lithuania","Luxembourg","Macedonia",
    "Madagascar","Malawi","Malaysia","Mali","Malta","Mauritania","Mauritius",
    "Mexico","Micronesia","Moldova","Mongolia","Montenegro","Montserrat","Morocco",
    "Mozambique","Myanmar","N. Cyprus","N. Mariana Is.","Namibia","Nepal",
    "Netherlands","New Caledonia","New Zealand","Nicaragua","Niger","Nigeria","Niue",
    "Norway","Oman","Pakistan","Palau","Palestine","Panama","Papua New Guinea",
    "Paraguay","Peru","Philippines","Poland","Portugal","Puerto Rico","Qatar",
    "Romania","Russia","Rwanda","S. Geo. and S. Sandw. Is.","S. Sudan","Saint Helena",
    "Saint Lucia","Samoa","São Tomé and Principe","Saudi Arabia","Senegal","Serbia",
    "Seychelles","Siachen Glacier","Sierra Leone","Singapore","Slovakia","Slovenia",
    "Solomon Is.","Somalia","South Africa","Spain","Sri Lanka",
    "St. Pierre and Miquelon","St. Vin. and Gren.","Sudan","Suriname","Swaziland",
    "Sweden","Switzerland","Syria","Tajikistan","Tanzania","Thailand","Timor-Leste",
    "Togo","Tonga","Trinidad and Tobago","Tunisia","Turkey","Turkmenistan",
    "Turks and Caicos Is.","U.S. Virgin Is.","Uganda","Ukraine","United Arab Emirates",
    "United Kingdom","United States","Uruguay","Uzbekistan","Vanuatu","Venezuela",
    "Vietnam","W. Sahara","Yemen","Zambia","Zimbabwe",
}

# Countries known to be absent from the GeoJSON entirely (geopolitical / dissolved)
NOT_IN_GEOJSON = {
    "Taiwan",           # shown as part of China in most world GeoJSONs
    "Hong Kong",        # shown as part of China
    "Hong Kong S.A.R.", # same
    "Federal Republic of Yugoslavia",  # dissolved 1992
}

# Must match WorldMap.tsx NAME_MAP exactly
NAME_MAP: dict[str, str] = {
    'Korea, Republic of':                           'Korea',
    'South Korea':                                  'Korea',
    "Korea (the Republic of)":                      'Korea',
    "Democratic People's Republic of Korea":        'Dem. Rep. Korea',
    'North Korea':                                  'Dem. Rep. Korea',
    'Viet Nam':                                     'Vietnam',
    'Russian Federation':                           'Russia',
    'Iran, Islamic Republic of':                    'Iran',
    'Iran (Islamic Republic of)':                   'Iran',
    'Syrian Arab Republic':                         'Syria',
    'Bolivia, Plurinational State of':              'Bolivia',
    'Venezuela, Bolivarian Republic of':            'Venezuela',
    'Tanzania, United Republic of':                 'Tanzania',
    "Lao People's Democratic Republic":             'Lao PDR',
    'Congo, the Democratic Republic of the':        'Dem. Rep. Congo',
    'Democratic Republic of the Congo':             'Dem. Rep. Congo',
    'Ivory Coast':                                  "Côte d'Ivoire",
    'Czech Republic':                               'Czech Rep.',
    'Czechia':                                      'Czech Rep.',
    'Taiwan, Province of China':                    'Taiwan',
    'Hong Kong':                                    'Hong Kong S.A.R.',
    'Macao':                                        'Macao S.A.R',
    'Moldova, Republic of':                         'Moldova',
    'Palestine, State of':                          'Palestine',
    'Macedonia, the Former Yugoslav Republic of':   'Macedonia',
    'United Kingdom of Great Britain and Northern Ireland': 'United Kingdom',
    'Turkey (Türkiye)':                        'Turkey',  # ü = U+00FC
    'Bosnia and Herzegovina':                       'Bosnia and Herz.',
    'Dominican Republic':                           'Dominican Rep.',
    'North Macedonia':                              'Macedonia',
    'The Gambia':                                   'Gambia',
    'The Bahamas':                                  'Bahamas',
    'Central African Republic':                     'Central African Rep.',
    'Equatorial Guinea':                            'Eq. Guinea',
    'Solomon Islands':                              'Solomon Is.',
}


def main():
    conn = db._db()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT trim(c) AS country, COUNT(DISTINCT nct_id) AS n
            FROM organized_trials,
              LATERAL unnest(string_to_array(COALESCE(countries, ''), ' | ')) AS c
            WHERE trim(c) != ''
            GROUP BY 1
            ORDER BY 2 DESC
        """)
        rows = cur.fetchall()

    print(f"Total distinct countries in DB: {len(rows)}\n")

    unmatched = []
    not_in_geo = []
    ok = []

    for r in rows:
        name = r["country"]
        mapped = NAME_MAP.get(name, name)
        if mapped in GEOJSON_NAMES:
            ok.append((name, r["n"], mapped))
        elif mapped in NOT_IN_GEOJSON or name in NOT_IN_GEOJSON:
            not_in_geo.append((name, r["n"]))
        else:
            unmatched.append((name, r["n"], mapped))

    if unmatched:
        print(f"ACTION NEEDED — {len(unmatched)} unmatched (add to WorldMap.tsx NAME_MAP):")
        for name, n, mapped in unmatched:
            print(f"  DB: {name!r:55} trials: {n:5}  tries: {mapped!r}")
    else:
        print("All mappable countries matched to GeoJSON features!")

    if not_in_geo:
        print(f"\nNot in GeoJSON (geopolitical limitation — can't fix):")
        for name, n in not_in_geo:
            print(f"  {name!r:55} trials: {n}")

    print(f"\nMatched OK: {len(ok)} countries")


if __name__ == "__main__":
    main()
