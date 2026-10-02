"""Tests for owned-fleet classification (Stellaris 4.x + legacy saves).

Stellaris 4.x ("Pegasus") tags every fleet with a ``ship_class`` and dropped the
old ``station=yes`` marker, so starbases (``shipclass_starbase``) were being
counted as military fleets/ships. Classification must key off ``ship_class`` when
present and fall back to the legacy ``station``/``civilian``/military-power
heuristic for older saves.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from augur.extractor.fleet_classification import classify_owned_fleet


def test_starbase_ship_class_is_starbase_even_without_station_flag():
    # 4.x starbase: no `station` field, single ship, non-trivial military power.
    fleet = {"ship_class": "shipclass_starbase", "military_power": "303", "ships": ["1"]}
    assert classify_owned_fleet(fleet) == "starbase"


def test_military_ship_class_is_military():
    fleet = {"ship_class": "shipclass_military", "military_power": "1233", "ships": ["1", "2"]}
    assert classify_owned_fleet(fleet) == "military"


def test_low_power_military_class_still_counts_as_military():
    # A battered fleet with low power is still military when ship_class says so.
    fleet = {"ship_class": "shipclass_military", "military_power": "12", "ships": ["1"]}
    assert classify_owned_fleet(fleet) == "military"


def test_civilian_ship_classes_are_civilian():
    for cls in (
        "shipclass_science_ship",
        "shipclass_constructor",
        "shipclass_mining_station",
        "shipclass_research_station",
        "shipclass_observation_station",
    ):
        fleet = {"ship_class": cls, "military_power": "0"}
        assert classify_owned_fleet(fleet) == "civilian", cls


def test_legacy_station_flag_is_starbase():
    fleet = {"station": "yes", "military_power": "303"}
    assert classify_owned_fleet(fleet) == "starbase"


def test_legacy_civilian_flag_is_civilian():
    fleet = {"civilian": "yes", "military_power": "0"}
    assert classify_owned_fleet(fleet) == "civilian"


def test_legacy_no_markers_uses_power_threshold():
    assert classify_owned_fleet({"military_power": "500"}) == "military"
    assert classify_owned_fleet({"military_power": "50"}) == "civilian"


def test_unknown_ship_class_falls_back_to_legacy_heuristic():
    # Unknown/future class with no station flag: treat by power heuristic.
    assert (
        classify_owned_fleet({"ship_class": "shipclass_future", "military_power": "500"})
        == "military"
    )
    # ...but a legacy station flag still wins.
    assert classify_owned_fleet({"ship_class": "shipclass_future", "station": "yes"}) == "starbase"
