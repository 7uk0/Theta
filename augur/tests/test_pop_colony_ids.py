"""Tests for player colony-id selection (Stellaris 4.x pop counting).

Stellaris 4.x pops reference colonies by the country's own colony ids
(``owned_planets``/``controlled_colonies``), which are a DIFFERENT id space from
the top-level ``planets.planet`` section keys. Scanning that section for
``owner==player_id`` lands on the wrong colonies (undercounting pops), so pop
statistics must select colonies from the country's colony list.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from augur.extractor.base import SaveExtractorBase


class _Dummy(SaveExtractorBase):
    """Minimal base double: stubs the two dependencies of _get_player_colony_ids."""

    def __init__(self, country_entry, legacy_ids=None):
        self._country_entry = country_entry
        self._legacy_ids = legacy_ids or []

    def get_player_empire_id(self):
        return 1

    def _get_player_country_entry(self, player_id=0):
        return self._country_entry

    def _get_player_planet_ids(self):
        return self._legacy_ids


def test_prefers_owned_planets_colony_ids():
    ext = _Dummy({"owned_planets": ["1", "62"]}, legacy_ids=["24", "325"])
    assert ext._get_player_colony_ids() == ["1", "62"]


def test_falls_back_to_controlled_colonies():
    ext = _Dummy({"controlled_colonies": ["3", "9"]}, legacy_ids=["24"])
    assert ext._get_player_colony_ids() == ["3", "9"]


def test_falls_back_to_legacy_owner_scan_when_no_colony_fields():
    ext = _Dummy({}, legacy_ids=["24", "325"])
    assert ext._get_player_colony_ids() == ["24", "325"]


def test_coerces_ids_to_strings():
    ext = _Dummy({"owned_planets": [1, 62]})
    assert ext._get_player_colony_ids() == ["1", "62"]
