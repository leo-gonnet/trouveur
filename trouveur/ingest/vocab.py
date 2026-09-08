"""The vocabularies derivation matches against. One file, on purpose.

A second copy of any of these -- a country map in the matcher, a seniority list in the web layer --
would disagree with this one, and every disagreement is silent: no error, just postings that
quietly fail to group or filter. If you need one of these lists somewhere else, import it.

Editing anything here changes derived output for postings already in the database, so it must be
accompanied by a DERIVE_VERSION bump. That is what makes the corpus re-derive itself.

Keys are folded (lowercase, diacritics stripped) to match trouveur.models.text.fold.
"""

from __future__ import annotations

from trouveur.models.facets import EmploymentType, Seniority, WorkMode

# Both the source's own spelling and the usual English/German names. Unlisted countries derive to
# nothing rather than to a guess.
COUNTRIES: dict[str, str] = {
    "deutschland": "DE", "germany": "DE", "allemagne": "DE", "de": "DE",
    "osterreich": "AT", "austria": "AT", "at": "AT",
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
    "rumanien": "RO", "romania": "RO",
    "bulgarien": "BG", "bulgaria": "BG",
    "danemark": "DK", "denmark": "DK",
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
)
# Every term here must be unambiguous about WHERE the work happens. "flexible" was removed after
# it classified a Bangalore role as hybrid by matching "flexible paid time off"; a benefits list
# is not a work-mode statement.
HYBRID_TERMS = ("hybrid", "teilweise remote", "hybrides arbeiten", "remote moglich")
ONSITE_TERMS = ("vor ort", "on-site", "onsite", "prasenz", "in office")

# Ordered most specific first: "senior" must win over a bare mention of "junior" elsewhere, and
# a leadership title must not be read as an ordinary senior role.
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

# What a source's own employment hint means. Source-agnostic tokens: normalisers map their own
# vocabulary onto these, so this table never grows a per-source branch.
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

# Deliberately short and curated. A skill list is a precision instrument: a term that also occurs
# as ordinary prose ("management", "design") tags half the corpus and stops discriminating.
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
