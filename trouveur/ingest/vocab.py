"""Every vocabulary derivation matches against, once. If you need one elsewhere, import it.

Editing anything here changes derived output for postings already stored, so it must come with a
DERIVE_VERSION bump. Keys are folded to match trouveur.models.text.fold.
"""

from __future__ import annotations

from trouveur.models.facets import EmploymentType, Seniority, WorkMode

# A German name with an umlaut needs BOTH spellings. Arbeitsagentur writes "OESTERREICH", which
# folds to "oesterreich", not to the "osterreich" that "Österreich" folds to -- keyed only on the
# umlauted form, every Austrian posting silently lost its country. 4.3% of a live sample.
COUNTRIES: dict[str, str] = {
    "deutschland": "DE", "germany": "DE", "allemagne": "DE", "de": "DE",
    "osterreich": "AT", "oesterreich": "AT", "austria": "AT", "at": "AT",
    "schweiz": "CH", "switzerland": "CH", "suisse": "CH", "ch": "CH",
    "luxemburg": "LU", "luxembourg": "LU",
    "niederlande": "NL", "netherlands": "NL", "holland": "NL",
    "belgien": "BE", "belgium": "BE",
    "frankreich": "FR", "france": "FR",
    "italien": "IT", "italy": "IT", "italia": "IT",
    "spanien": "ES", "spain": "ES", "espana": "ES",
    "portugal": "PT",
    "polen": "PL", "poland": "PL",
    "tschechien": "CZ", "czechia": "CZ", "czech republic": "CZ",
    "slowakei": "SK", "slovakia": "SK",
    "slowenien": "SI", "slovenia": "SI",
    "ungarn": "HU", "hungary": "HU",
    "kroatien": "HR", "croatia": "HR",
    "rumanien": "RO", "rumaenien": "RO", "romania": "RO",
    "bulgarien": "BG", "bulgaria": "BG",
    "danemark": "DK", "daenemark": "DK", "denmark": "DK",
    "schweden": "SE", "sweden": "SE",
    "norwegen": "NO", "norway": "NO",
    "finnland": "FI", "finland": "FI",
    "irland": "IE", "ireland": "IE",
    "grossbritannien": "GB", "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "griechenland": "GR", "greece": "GR",
    "estland": "EE", "estonia": "EE",
    "lettland": "LV", "latvia": "LV",
    "litauen": "LT", "lithuania": "LT",
    "usa": "US", "united states": "US", "us": "US", "vereinigte staaten": "US",
    "kanada": "CA", "canada": "CA",
    "indien": "IN", "india": "IN",
    "israel": "IL",
    "singapur": "SG", "singapore": "SG",
    "australien": "AU", "australia": "AU",
    "japan": "JP", "brasilien": "BR", "brazil": "BR",
    "turkei": "TR", "tuerkei": "TR", "turkey": "TR",
    "sudafrika": "ZA", "suedafrika": "ZA", "south africa": "ZA",
    # Every other country, by its GeoNames English name. A posting abroad that derives to no
    # country passes every location filter, so a missing name here is a job in China shown to
    # someone who asked for Vienna. "Georgia" is deliberately absent: "Atlanta, Georgia" is a
    # US state far more often than the country, and read as GE it would move those postings.
    "afghanistan": "AF", "aland islands": "AX", "albania": "AL", "albanien": "AL", "algeria": "DZ",
    "algerien": "DZ", "american samoa": "AS", "andorra": "AD", "angola": "AO", "anguilla": "AI",
    "antarctica": "AQ", "antigua and barbuda": "AG", "argentina": "AR", "argentinien": "AR",
    "armenia": "AM", "aruba": "AW", "azerbaijan": "AZ", "bahamas": "BS", "bahrain": "BH",
    "bangladesh": "BD", "barbados": "BB", "belarus": "BY", "weissrussland": "BY", "belize": "BZ",
    "benin": "BJ", "bermuda": "BM", "bhutan": "BT", "bolivia": "BO", "bosnia and herzegovina": "BA",
    "bosnien und herzegowina": "BA", "botswana": "BW", "bouvet island": "BV",
    "british indian ocean territory": "IO", "british virgin islands": "VG", "brunei": "BN",
    "burkina faso": "BF", "burundi": "BI", "cabo verde": "CV", "cambodia": "KH", "cameroon": "CM",
    "cayman islands": "KY", "central african republic": "CF", "chad": "TD", "chile": "CL",
    "china": "CN", "volksrepublik china": "CN", "christmas island": "CX", "cocos islands": "CC",
    "colombia": "CO", "kolumbien": "CO", "comoros": "KM", "cook islands": "CK", "costa rica": "CR",
    "cuba": "CU", "curacao": "CW", "cyprus": "CY", "zypern": "CY",
    "democratic republic of the congo": "CD", "djibouti": "DJ", "dominica": "DM",
    "dominican republic": "DO", "ecuador": "EC", "egypt": "EG", "agypten": "EG", "aegypten": "EG",
    "el salvador": "SV", "equatorial guinea": "GQ", "eritrea": "ER", "eswatini": "SZ",
    "ethiopia": "ET", "falkland islands": "FK", "faroe islands": "FO", "fiji": "FJ",
    "french guiana": "GF", "french polynesia": "PF", "french southern territories": "TF",
    "gabon": "GA", "gambia": "GM", "ghana": "GH", "gibraltar": "GI", "greenland": "GL",
    "grenada": "GD", "guadeloupe": "GP", "guam": "GU", "guatemala": "GT", "guernsey": "GG",
    "guinea": "GN", "guinea- bissau": "GW", "guyana": "GY", "haiti": "HT",
    "heard island and mcdonald islands": "HM", "honduras": "HN", "hong kong": "HK", "iceland": "IS",
    "island": "IS", "indonesia": "ID", "indonesien": "ID", "iran": "IR", "iraq": "IQ",
    "isle of man": "IM", "ivory coast": "CI", "cote d'ivoire": "CI", "jamaica": "JM",
    "jersey": "JE", "jordan": "JO", "kazakhstan": "KZ", "kasachstan": "KZ", "kenya": "KE",
    "kiribati": "KI", "kosovo": "XK", "kuwait": "KW", "kyrgyzstan": "KG", "laos": "LA",
    "lebanon": "LB", "lesotho": "LS", "liberia": "LR", "libya": "LY", "liechtenstein": "LI",
    "macao": "MO", "madagascar": "MG", "malawi": "MW", "malaysia": "MY", "maldives": "MV",
    "mali": "ML", "malta": "MT", "marshall islands": "MH", "martinique": "MQ", "mauritania": "MR",
    "mauritius": "MU", "mayotte": "YT", "mexico": "MX", "mexiko": "MX", "micronesia": "FM",
    "moldova": "MD", "moldau": "MD", "republik moldau": "MD", "monaco": "MC", "mongolia": "MN",
    "montenegro": "ME", "montserrat": "MS", "morocco": "MA", "marokko": "MA", "mozambique": "MZ",
    "myanmar": "MM", "namibia": "NA", "nauru": "NR", "nepal": "NP", "new caledonia": "NC",
    "new zealand": "NZ", "neuseeland": "NZ", "nicaragua": "NI", "niger": "NE", "nigeria": "NG",
    "niue": "NU", "norfolk island": "NF", "north korea": "KP", "north macedonia": "MK",
    "nordmazedonien": "MK", "northern mariana islands": "MP", "oman": "OM", "pakistan": "PK",
    "palau": "PW", "palestinian territory": "PS", "panama": "PA", "papua new guinea": "PG",
    "paraguay": "PY", "peru": "PE", "philippines": "PH", "philippinen": "PH", "pitcairn": "PN",
    "puerto rico": "PR", "qatar": "QA", "katar": "QA", "republic of the congo": "CG",
    "reunion": "RE", "russia": "RU", "russland": "RU", "russian federation": "RU", "rwanda": "RW",
    "saint barthelemy": "BL", "saint helena": "SH", "saint kitts and nevis": "KN",
    "saint lucia": "LC", "saint martin": "MF", "saint pierre and miquelon": "PM",
    "saint vincent and the grenadines": "VC", "samoa": "WS", "san marino": "SM",
    "sao tome and principe": "ST", "saudi arabia": "SA", "saudi-arabien": "SA", "senegal": "SN",
    "serbia": "RS", "serbien": "RS", "seychelles": "SC", "sierra leone": "SL", "sint maarten": "SX",
    "solomon islands": "SB", "somalia": "SO", "south georgia and the south sandwich islands": "GS",
    "south korea": "KR", "korea": "KR", "sudkorea": "KR", "suedkorea": "KR", "south sudan": "SS",
    "sri lanka": "LK", "sudan": "SD", "suriname": "SR", "svalbard and jan mayen": "SJ",
    "syria": "SY", "taiwan": "TW", "tajikistan": "TJ", "tanzania": "TZ", "thailand": "TH",
    "timor leste": "TL", "togo": "TG", "tokelau": "TK", "tonga": "TO", "trinidad and tobago": "TT",
    "tunisia": "TN", "tunesien": "TN", "turkmenistan": "TM", "turks and caicos islands": "TC",
    "tuvalu": "TV", "u.s. virgin islands": "VI", "uganda": "UG", "ukraine": "UA",
    "united arab emirates": "AE", "uae": "AE", "vereinigte arabische emirate": "AE",
    "united states minor outlying islands": "UM", "uruguay": "UY", "uzbekistan": "UZ",
    "vanuatu": "VU", "vatican": "VA", "venezuela": "VE", "vietnam": "VN", "viet nam": "VN",
    "wallis and futuna": "WF", "western sahara": "EH", "yemen": "YE", "zambia": "ZM",
    "zimbabwe": "ZW",
}

# Arbeitsagentur writes German federal states in screaming snake case.
REGIONS: dict[str, str] = {
    "baden_wuerttemberg": "Baden-Württemberg",
    "bayern": "Bayern",
    "berlin": "Berlin",
    "brandenburg": "Brandenburg",
    "bremen": "Bremen",
    "hamburg": "Hamburg",
    "hessen": "Hessen",
    "mecklenburg_vorpommern": "Mecklenburg-Vorpommern",
    "niedersachsen": "Niedersachsen",
    "nordrhein_westfalen": "Nordrhein-Westfalen",
    "rheinland_pfalz": "Rheinland-Pfalz",
    "saarland": "Saarland",
    "sachsen": "Sachsen",
    "sachsen_anhalt": "Sachsen-Anhalt",
    "schleswig_holstein": "Schleswig-Holstein",
    "thueringen": "Thüringen",
}

REMOTE_TERMS = (
    "remote", "homeoffice", "home office", "telearbeit", "ortsunabhangig",
    "work from home", "fully remote", "100% remote", "vollstandig remote",
    # A board naming its work location in words needs the word here, or it is read as a city --
    # Jobicy postings derived a city of "Anywhere".
    "anywhere", "worldwide", "weltweit", "anywhere in the world", "global",
)
# Every term must be unambiguous about WHERE the work happens: "flexible" was removed after it
# read "flexible paid time off" as hybrid.
HYBRID_TERMS = ("hybrid", "teilweise remote", "hybrides arbeiten", "remote moglich")
ONSITE_TERMS = ("vor ort", "on-site", "onsite", "prasenz", "in office")

# Ordered most specific first: a leadership title must not be read as an ordinary senior role.
SENIORITY_TERMS: tuple[tuple[str, Seniority], ...] = (
    ("praktikant", Seniority.INTERN),
    ("praktikum", Seniority.INTERN),
    ("werkstudent", Seniority.INTERN),
    ("intern ", Seniority.INTERN),
    ("internship", Seniority.INTERN),
    ("working student", Seniority.INTERN),
    ("chief ", Seniority.EXECUTIVE),
    ("geschaftsfuhrer", Seniority.EXECUTIVE),
    ("vorstand", Seniority.EXECUTIVE),
    ("vice president", Seniority.EXECUTIVE),
    ("director", Seniority.EXECUTIVE),
    ("head of", Seniority.LEAD),
    ("leiter", Seniority.LEAD),
    ("leitung", Seniority.LEAD),
    ("teamlead", Seniority.LEAD),
    ("team lead", Seniority.LEAD),
    ("principal", Seniority.LEAD),
    ("staff ", Seniority.LEAD),
    ("senior", Seniority.SENIOR),
    ("erfahren", Seniority.SENIOR),
    ("junior", Seniority.JUNIOR),
    ("einsteiger", Seniority.JUNIOR),
    ("berufseinsteiger", Seniority.JUNIOR),
    ("graduate", Seniority.JUNIOR),
    ("trainee", Seniority.JUNIOR),
)

EMPLOYMENT_TERMS: tuple[tuple[str, EmploymentType], ...] = (
    ("ausbildung", EmploymentType.APPRENTICESHIP),
    ("auszubildende", EmploymentType.APPRENTICESHIP),
    ("duales studium", EmploymentType.APPRENTICESHIP),
    ("apprentice", EmploymentType.APPRENTICESHIP),
    ("praktikum", EmploymentType.INTERNSHIP),
    ("internship", EmploymentType.INTERNSHIP),
    ("werkstudent", EmploymentType.INTERNSHIP),
    ("befristet", EmploymentType.TEMPORARY),
    ("temporary", EmploymentType.TEMPORARY),
    ("zeitarbeit", EmploymentType.TEMPORARY),
    ("freelance", EmploymentType.CONTRACT),
    ("freiberuflich", EmploymentType.CONTRACT),
    ("contractor", EmploymentType.CONTRACT),
    ("teilzeit", EmploymentType.PART_TIME),
    ("part-time", EmploymentType.PART_TIME),
    ("part time", EmploymentType.PART_TIME),
    ("vollzeit", EmploymentType.FULL_TIME),
    ("full-time", EmploymentType.FULL_TIME),
    ("full time", EmploymentType.FULL_TIME),
)

EMPLOYMENT_HINTS: dict[str, EmploymentType] = {
    "vollzeit": EmploymentType.FULL_TIME,
    "teilzeit": EmploymentType.PART_TIME,
    "geringfuegig": EmploymentType.PART_TIME,
}

WORK_MODE_BY_TERM: tuple[tuple[tuple[str, ...], WorkMode], ...] = (
    (HYBRID_TERMS, WorkMode.HYBRID),
    (REMOTE_TERMS, WorkMode.REMOTE),
    (ONSITE_TERMS, WorkMode.ONSITE),
)

# Deliberately short: a term that also occurs as ordinary prose ("management", "design") tags
# half the corpus and stops discriminating.
SKILLS: tuple[str, ...] = (
    "python", "java", "javascript", "typescript", "golang", "rust", "c++", "c#", "kotlin",
    "scala", "ruby", "php", "matlab", "labview", "vba", "sql", "nosql",
    "react", "angular", "vue", "django", "fastapi", "spring boot", "node.js",
    "postgresql", "mysql", "oracle", "mongodb", "redis", "elasticsearch", "snowflake",
    "kubernetes", "docker", "terraform", "ansible", "jenkins", "gitlab ci", "github actions",
    "aws", "azure", "gcp", "openshift",
    "sap", "sap mm", "sap sd", "sap pp", "abap", "salesforce", "dynamics",
    "autocad", "solidworks", "catia", "creo", "inventor", "ansys", "abaqus", "comsol",
    "simulink", "plc", "sps", "siemens s7", "tia portal", "codesys", "profinet", "canbus",
    "lean management", "six sigma", "kaizen", "kanban", "scrum", "prince2", "pmp", "itil",
    "iso 9001", "iso 14001", "iatf 16949", "gmp", "fmea", "apqp", "ppap", "8d",
    "machine learning", "deep learning", "pytorch", "tensorflow", "scikit-learn",
    "power bi", "tableau", "qlik", "looker", "dbt", "airflow", "spark", "hadoop", "kafka",
    "hplc", "gc-ms", "spektroskopie", "cad", "cam", "cnc", "spc", "msa",
)
