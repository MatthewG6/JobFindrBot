import re


TWIN_CITIES_SEVEN_COUNTY_LOCALITIES = frozenset(
    {
        "afton",
        "andover",
        "anoka",
        "anoka county",
        "apple valley",
        "arden hills",
        "bayport",
        "belle plaine",
        "bethel",
        "birchwood village",
        "blaine",
        "bloomington",
        "brooklyn center",
        "brooklyn park",
        "burnsville",
        "carver",
        "carver county",
        "centerville",
        "champlin",
        "chanhassen",
        "chaska",
        "circle pines",
        "coates",
        "cologne",
        "columbia heights",
        "columbus",
        "coon rapids",
        "corcoran",
        "cottage grove",
        "credit river",
        "crystal",
        "dakota county",
        "dayton",
        "deephaven",
        "dellwood",
        "eagan",
        "east bethel",
        "eden prairie",
        "edina",
        "elko new market",
        "empire",
        "excelsior",
        "falcon heights",
        "farmington",
        "forest lake",
        "fridley",
        "gem lake",
        "golden valley",
        "grant",
        "greenfield",
        "greenwood",
        "ham lake",
        "hamburg",
        "hampton",
        "hastings",
        "hennepin county",
        "hilltop",
        "hopkins",
        "hugo",
        "independence",
        "inver grove heights",
        "jordan",
        "lake elmo",
        "lake st croix beach",
        "lakeland",
        "lakeland shores",
        "lakeville",
        "landfall",
        "lauderdale",
        "lexington",
        "lilydale",
        "lino lakes",
        "little canada",
        "long lake",
        "loretto",
        "mahtomedi",
        "maple grove",
        "maple plain",
        "maplewood",
        "marine on st croix",
        "mayer",
        "medicine lake",
        "medina",
        "mendota",
        "mendota heights",
        "miesville",
        "minneapolis",
        "minnetonka",
        "minnetonka beach",
        "minnetrista",
        "mound",
        "mounds view",
        "new brighton",
        "new germany",
        "new hope",
        "new market",
        "new trier",
        "newport",
        "north oaks",
        "north st paul",
        "norwood young america",
        "nowthen",
        "oak grove",
        "oak park heights",
        "oakdale",
        "orono",
        "osseo",
        "pine springs",
        "plymouth",
        "prior lake",
        "ramsey",
        "ramsey county",
        "randolph",
        "richfield",
        "robbinsdale",
        "rogers",
        "rosemount",
        "roseville",
        "saint paul",
        "san francisco township",
        "savage",
        "scandia",
        "scott county",
        "shakopee",
        "shoreview",
        "shorewood",
        "south st paul",
        "spring lake park",
        "spring park",
        "st anthony village",
        "st bonifacius",
        "st francis",
        "st louis park",
        "st marys point",
        "st paul",
        "st paul park",
        "stillwater",
        "sunfish lake",
        "tonka bay",
        "twin cities",
        "vadnais heights",
        "vermillion",
        "victoria",
        "waconia",
        "washington county",
        "watertown",
        "wayzata",
        "west st paul",
        "white bear lake",
        "willernie",
        "woodbury",
        "woodland",
    }
)
STATE_NAMES = frozenset(
    {
        "alabama",
        "alaska",
        "arizona",
        "arkansas",
        "california",
        "colorado",
        "connecticut",
        "delaware",
        "florida",
        "georgia",
        "hawaii",
        "idaho",
        "illinois",
        "indiana",
        "iowa",
        "kansas",
        "kentucky",
        "louisiana",
        "maine",
        "maryland",
        "massachusetts",
        "michigan",
        "minnesota",
        "mississippi",
        "missouri",
        "montana",
        "nebraska",
        "nevada",
        "new hampshire",
        "new jersey",
        "new mexico",
        "new york",
        "north carolina",
        "north dakota",
        "ohio",
        "oklahoma",
        "oregon",
        "pennsylvania",
        "rhode island",
        "south carolina",
        "south dakota",
        "tennessee",
        "texas",
        "utah",
        "vermont",
        "virginia",
        "washington",
        "west virginia",
        "wisconsin",
        "wyoming",
    }
)
STATE_ABBREVIATIONS = frozenset(
    {
        "al",
        "ak",
        "az",
        "ar",
        "ca",
        "co",
        "ct",
        "de",
        "fl",
        "ga",
        "hi",
        "id",
        "il",
        "in",
        "ia",
        "ks",
        "ky",
        "la",
        "me",
        "md",
        "ma",
        "mi",
        "mn",
        "ms",
        "mo",
        "mt",
        "ne",
        "nv",
        "nh",
        "nj",
        "nm",
        "ny",
        "nc",
        "nd",
        "oh",
        "ok",
        "or",
        "pa",
        "ri",
        "sc",
        "sd",
        "tn",
        "tx",
        "ut",
        "vt",
        "va",
        "wa",
        "wv",
        "wi",
        "wy",
    }
)
FOREIGN_LOCATION_MARKERS = frozenset(
    {
        "alberta",
        "british columbia",
        "canada",
        "england",
        "manitoba",
        "new brunswick",
        "newfoundland",
        "nova scotia",
        "ontario",
        "quebec",
        "saskatchewan",
        "scotland",
        "united kingdom",
        "wales",
    }
)


def normalized_place(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def explicit_state(value: str, locality: str) -> str | None:
    for segment in value.split(",")[1:]:
        normalized = normalized_place(segment)
        words = normalized.split()
        for state in STATE_NAMES:
            if state in normalized:
                return state
        first = words[0] if words else ""
        if first in STATE_ABBREVIATIONS:
            return first
    normalized = normalized_place(value)
    without_locality = normalized.replace(locality, " ", 1)
    if any(
        f" {marker} " in f" {without_locality} "
        for marker in FOREIGN_LOCATION_MARKERS
    ):
        return "foreign"
    for state in STATE_NAMES:
        if state != "minnesota" and f" {state} " in f" {without_locality} ":
            return state
    locality_position = normalized.find(locality)
    if locality_position >= 0:
        suffix = normalized[locality_position + len(locality) :].split()
        if suffix and suffix[0] in STATE_ABBREVIATIONS:
            return suffix[0]
    return None


def is_twin_cities_seven_county_location(value: str) -> bool:
    normalized = f" {normalized_place(value)} "
    for locality in TWIN_CITIES_SEVEN_COUNTY_LOCALITIES:
        if f" {locality} " not in normalized:
            continue
        state = explicit_state(value, locality)
        if state in {None, "mn", "minnesota"}:
            return True
    return False


def is_explicit_foreign_location(value: str) -> bool:
    normalized = f" {normalized_place(value)} "
    return any(
        f" {marker} " in normalized for marker in FOREIGN_LOCATION_MARKERS
    )


def is_us_location(value: str) -> bool:
    if is_explicit_foreign_location(value):
        return False
    normalized = normalized_place(value)
    padded = f" {normalized} "
    if any(
        marker in padded
        for marker in (" united states ", " usa ", " us ", " u s ")
    ):
        return True
    if any(f" {state} " in padded for state in STATE_NAMES):
        return True
    segments = [normalized_place(segment) for segment in value.split(",")]
    suffix_words = [word for segment in segments[1:] for word in segment.split()]
    if any(word in STATE_ABBREVIATIONS for word in suffix_words):
        return True
    words = normalized.split()
    return bool(words and words[-1] in STATE_ABBREVIATIONS)


def is_rochester_minnesota_location(value: str) -> bool:
    normalized = f" {normalized_place(value)} "
    if " rochester " not in normalized:
        return False
    state = explicit_state(value, "rochester")
    return state in {None, "mn", "minnesota"}
