"""Regression tests for Stellaris 4.4 Colony/Carrier separation."""

from __future__ import annotations

from augur.extractor.base import SaveExtractorBase
from augur.extractor.colony_resolver import (
    ColonyRef,
    ColonyResolution,
    candidate_carrier_ids,
    resolve_player_colonies,
)
from augur.extractor.planets import PlanetsMixin
from augur.extractor.player import PlayerMixin


def test_legacy_save_keeps_planet_and_colony_ids_identical():
    resolution = resolve_player_colonies(
        country={"owned_planets": [24, 325]},
        player_id=1,
        colonies_section={},
        planets_section={
            "planet": {
                "24": {"owner": "1", "name": {"key": "NAME_Alpha"}},
                "325": {"owner": "1", "name": {"key": "NAME_Beta"}},
                "900": {"owner": "2"},
            }
        },
    )

    assert resolution.schema == "legacy_planet"
    assert [ref.colony_id for ref in resolution.refs] == ["24", "325"]
    assert [ref.carrier_id for ref in resolution.refs] == ["24", "325"]
    assert all(ref.carrier_type == "planet" for ref in resolution.refs)


def test_44_settled_colonies_resolve_separate_planet_carriers():
    resolution = resolve_player_colonies(
        country={"owned_planets": [24, 325], "controlled_colonies": [1, 62]},
        player_id=1,
        colonies_section={
            "1": {"carrier": {"type": "planet", "reference": 24}, "stability": "72"},
            "62": {"carrier": {"type": "planet", "reference": 325}, "stability": "81"},
        },
        planets_section={
            "planet": {
                "24": {"name": {"key": "NAME_Alpha"}, "planet_class": "pc_ocean"},
                "325": {"name": {"key": "NAME_Beta"}, "planet_class": "pc_desert"},
            }
        },
    )

    assert resolution.schema == "colony_carrier"
    assert [(ref.colony_id, ref.carrier_id) for ref in resolution.refs] == [
        ("1", "24"),
        ("62", "325"),
    ]
    assert all(ref.carrier_type == "planet" for ref in resolution.refs)
    assert resolution.refs[0].colony["stability"] == "72"
    assert resolution.refs[0].carrier["planet_class"] == "pc_ocean"


def test_44_ship_colony_resolves_arkship_carrier():
    resolution = resolve_player_colonies(
        country={"controlled_colonies": [7]},
        player_id=1,
        colonies_section={
            "colony": {
                "7": {
                    "carrier": {"type": "ship", "reference": 900},
                    "planet_class": "pc_ark",
                }
            }
        },
        planets_section={"planet": {}},
        ships_section={
            "900": {
                "name": {"key": "NAME_Wanderer"},
                "ship_class": "shipclass_arkship",
            }
        },
    )

    assert len(resolution.refs) == 1
    ref = resolution.refs[0]
    assert ref.colony_id == "7"
    assert ref.carrier_type == "ship"
    assert ref.carrier_id == "900"
    assert ref.carrier["ship_class"] == "shipclass_arkship"


def test_numeric_carrier_type_is_inferred_from_section_membership():
    resolution = resolve_player_colonies(
        country={"controlled_colonies": [7]},
        player_id=1,
        colonies_section={"7": {"carrier": {"type": 2, "id": 900}}},
        planets_section={"planet": {}},
        ships_section={"900": {"ship_class": "shipclass_arkship"}},
    )

    assert resolution.refs[0].carrier_type == "ship"
    assert resolution.refs[0].carrier_id == "900"


def test_missing_carrier_is_visible_as_a_compatibility_warning():
    resolution = resolve_player_colonies(
        country={"controlled_colonies": [7]},
        player_id=1,
        colonies_section={"7": {"stability": "50"}},
        planets_section={"planet": {}},
    )

    assert len(resolution.refs) == 1
    assert resolution.refs[0].carrier_type == "unknown"
    assert resolution.warnings == ("Player colony 7 has no resolvable carrier",)


def test_missing_controlled_colony_falls_back_to_owned_planet_carrier():
    resolution = resolve_player_colonies(
        country={"controlled_colonies": [999], "owned_planets": [24]},
        player_id=1,
        colonies_section={"1": {"carrier": {"type": "planet", "id": 24}, "stability": "72"}},
        planets_section={"planet": {"24": {"name": {"key": "NAME_Alpha"}}}},
    )

    assert [ref.colony_id for ref in resolution.refs] == ["1"]
    assert resolution.refs[0].carrier_id == "24"
    assert resolution.warnings == ("Player colony 999 is absent from the colonies section",)


def test_candidate_carrier_ids_avoid_loading_the_entire_ship_section():
    candidates = candidate_carrier_ids(
        {
            "1": {"carrier": {"type": "planet", "id": 24}},
            "7": {"carrier": {"type": "ship", "id": 900}},
        }
    )

    # Fetching the planet ID as well is harmless: get_entries("ships", ...) skips
    # absent keys and protects against overlapping numeric ID spaces.
    assert candidates == ["24", "900"]


class _PegasusSessionDouble:
    def extract_sections(self, sections):
        assert sections == ["colony", "colonies", "ship_colonies", "planets"]
        return {
            "colony": {
                "0": {
                    "carrier": {"type": "planet", "reference": "8"},
                    "num_sapient_pops": "5436",
                }
            },
            "planets": {
                "planet": {
                    "8": {
                        "colony": "0",
                        "owner": "0",
                        "planet_class": "pc_ocean",
                    }
                }
            },
        }

    def get_entries(self, section, ids):
        assert section == "ships"
        assert ids == ["8"]
        return []


class _BaseDouble(SaveExtractorBase):
    def __init__(self):
        self._player_colony_resolution_cache = None

    def get_player_empire_id(self):
        return 0

    def _get_player_country_entry(self, _player_id):
        return {"owned_planets": [0], "controlled_colonies": [0]}


def test_base_reads_real_pegasus_singular_colony_section(monkeypatch):
    from augur.extractor import base as base_module

    session = _PegasusSessionDouble()
    monkeypatch.setattr(base_module, "_get_active_session", lambda: session)

    resolution = _BaseDouble()._get_player_colony_resolution()

    assert resolution.schema == "colony_carrier"
    assert len(resolution.refs) == 1
    assert resolution.refs[0].colony_id == "0"
    assert resolution.refs[0].carrier_type == "planet"
    assert resolution.refs[0].carrier_id == "8"


class _PlanetsDouble(PlanetsMixin):
    _planets_cache = None

    def __init__(self, resolution):
        self._resolution = resolution

    def _get_player_colony_resolution(self):
        return self._resolution

    def _get_population_by_planet_rust(self):
        return {1: 120, 7: 80}

    def _get_building_types(self):
        return {"5": "building_research_lab"}

    def _get_country_names_map(self):
        return {}

    def _extract_planet_name(self, value):
        return value["key"].removeprefix("NAME_")


def test_get_planets_uses_colony_state_and_supports_ship_carriers(monkeypatch):
    from augur.extractor import planets as planets_module

    monkeypatch.setattr(planets_module, "_get_active_session", lambda: object())
    resolution = ColonyResolution(
        refs=(
            ColonyRef(
                colony_id="1",
                colony={"stability": "72", "districts": [1, 2], "buildings_cache": [5]},
                carrier_type="planet",
                carrier_id="24",
                carrier={"name": {"key": "NAME_Alpha"}, "planet_class": "pc_ocean"},
                schema="colony_carrier",
            ),
            ColonyRef(
                colony_id="7",
                colony={"planet_class": "pc_ark", "stability": "65"},
                carrier_type="ship",
                carrier_id="900",
                carrier={"name": {"key": "NAME_Wanderer"}},
                schema="colony_carrier",
            ),
        ),
        schema="colony_carrier",
    )

    result = _PlanetsDouble(resolution).get_planets()

    assert result["count"] == 2
    assert result["total_pops"] == 200
    assert result["planet_colonies"] == 1
    assert result["ship_colonies"] == 1
    assert result["planets"][0] == {
        "id": "24",
        "colony_id": "1",
        "carrier_type": "planet",
        "carrier_id": "24",
        "name": "Alpha",
        "type": "ocean",
        "population": 120,
        "stability": 72.0,
        "buildings": ["research_lab"],
        "district_count": 2,
    }
    assert result["planets"][1]["id"] == "900"
    assert result["planets"][1]["name"] == "Wanderer"
    assert result["planets"][1]["type"] == "ark"
    assert result["planets"][1]["population"] == 80


class _SessionDouble:
    def extract_sections(self, sections):
        assert sections == ["pop_jobs"]
        return {"pop_jobs": {"11": {"type": "soldier", "workforce": "100"}}}


class _PlayerDouble(PlayerMixin):
    def _get_player_colony_resolution(self):
        return ColonyResolution(
            refs=(
                ColonyRef(
                    colony_id="7",
                    colony={"pop_jobs": [11]},
                    carrier_type="ship",
                    carrier_id="900",
                    carrier={},
                    schema="colony_carrier",
                ),
            ),
            schema="colony_carrier",
        )


def test_naval_cap_jobs_include_ship_carried_colonies(monkeypatch):
    from augur.extractor import player as player_module

    monkeypatch.setattr(player_module, "_get_active_session", lambda: _SessionDouble())
    result = _PlayerDouble()._get_naval_cap_job_analysis(
        player_country={},
        civics=set(),
        country_flags=set(),
        researched_techs=set(),
    )

    assert result["flat_additions"] == {"Soldier jobs": 2.0}
    assert result["unresolved_source_families"] == set()
