"""Tests for diplomacy 'known empires' counting (Stellaris 4.x).

The player's relations_manager holds a relation record for every country it knows
about — including non-empire entities (enclaves, marauders, drones, primitives,
internal factions). Counting all relations over-states "known empires" (e.g. 13
records vs 8 actual empires). We segment by the target country's `type`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from augur.extractor.diplomacy import (
    _is_diplomatic_empire,
    _summarize_relation_types,
)


def test_is_diplomatic_empire_recognizes_empire_types():
    assert _is_diplomatic_empire("default")
    assert _is_diplomatic_empire("fallen_empire")
    assert _is_diplomatic_empire("awakened_fallen_empire")


def test_is_diplomatic_empire_rejects_special_entities():
    for t in ("enclave", "dormant_marauders", "drone", "primitive", "nice_faction", None):
        assert not _is_diplomatic_empire(t)


def test_summarize_relation_types_counts_empires_and_breakdown():
    relations = [
        {"country_type": "default"},
        {"country_type": "default"},
        {"country_type": "fallen_empire"},
        {"country_type": "enclave"},
        {"country_type": "dormant_marauders"},
        {"country_type": "primitive"},
    ]

    summary = _summarize_relation_types(relations)

    assert summary["empire_count"] == 3
    assert summary["by_type"]["default"] == 2
    assert summary["by_type"]["enclave"] == 1
    assert summary["by_type"]["primitive"] == 1


def test_summarize_handles_missing_type_as_unknown():
    summary = _summarize_relation_types([{"country_type": None}, {}])
    assert summary["empire_count"] == 0
    assert summary["by_type"]["unknown"] == 2
