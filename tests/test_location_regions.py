import pytest

from app.location_regions import is_twin_cities_seven_county_location


@pytest.mark.parametrize(
    "location",
    [
        "Andover, Minnesota",
        "Wayzata, MN",
        "Champlin, MN",
        "Minnetrista, MN",
        "Eagan, Minnesota",
        "Shakopee, Scott County",
        "Twin Cities",
    ],
)
def test_twin_cities_region_recognizes_metro_localities(location: str) -> None:
    assert is_twin_cities_seven_county_location(location) is True


@pytest.mark.parametrize(
    "location",
    [
        "Minneapolis, Kansas",
        "Minneapolis, KS",
        "Minneapolis Kansas",
        "Minneapolis KS",
        "Minneapolis (Kansas)",
        "Minneapolis, Kansas 67467",
        "Washington County, Wisconsin",
        "Washington County WI",
        "Shakopee, Iowa",
        "St. Paul, Alberta, Canada",
        "Minneapolis, Ontario, Canada",
        "Woodbury, England, United Kingdom",
        "Rochester, MN",
        "Madison, WI",
    ],
)
def test_twin_cities_region_rejects_explicit_out_of_region_locations(
    location: str,
) -> None:
    assert is_twin_cities_seven_county_location(location) is False


@pytest.mark.parametrize(
    "location",
    ["Remote - US", "Remote, US", "US Remote", "Remote - U.S."],
)
def test_us_location_recognizes_common_country_aliases(location: str) -> None:
    from app.location_regions import is_us_location

    assert is_us_location(location) is True
