"""Tests for the extraction validation module.

Tests the ValidationResult dataclass and validate_all() integration.
"""

import os
import sys

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from augur.extractor.validation import ExtractionValidator, ValidationResult

# Path to test save fixture
TEST_SAVE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_save.sav"
)


@pytest.fixture
def test_save_path():
    """Return path to test save file."""
    if not os.path.exists(TEST_SAVE_PATH):
        pytest.skip(f"Test save file not found at {TEST_SAVE_PATH}")
    return TEST_SAVE_PATH


@pytest.fixture
def validator(test_save_path):
    """Create a validator instance with Rust session."""
    try:
        from augur.parser.rust_bridge import session as rust_session
    except ImportError:
        pytest.skip("Rust bridge not available")
        return

    with rust_session(test_save_path):
        validator = ExtractionValidator(test_save_path)
        yield validator


class TestValidationResult:
    """Tests for the ValidationResult dataclass."""

    def test_initial_state(self):
        """ValidationResult starts as valid with empty collections."""
        result = ValidationResult(valid=True)
        assert result.valid is True
        assert result.issues == []
        assert result.warnings == []
        assert result.checks_passed == 0
        assert result.checks_failed == 0

    def test_add_issue_sets_invalid(self):
        """Adding an issue marks the result as invalid."""
        result = ValidationResult(valid=True)
        result.add_issue("test_check", "Test message")

        assert result.valid is False
        assert len(result.issues) == 1
        assert result.checks_failed == 1

    def test_add_issue_with_details(self):
        """Issues can include details and fix suggestions."""
        result = ValidationResult(valid=True)
        result.add_issue(
            "test_check", "Test message", details={"key": "value"}, fix_suggestion="Try this fix"
        )

        issue = result.issues[0]
        assert issue["check"] == "test_check"
        assert issue["message"] == "Test message"
        assert issue["details"] == {"key": "value"}
        assert issue["fix_suggestion"] == "Try this fix"

    def test_add_warning_keeps_valid(self):
        """Warnings don't mark the result as invalid."""
        result = ValidationResult(valid=True)
        result.add_warning("test_warning", "Warning message")

        assert result.valid is True
        assert len(result.warnings) == 1
        assert result.checks_warned == 1

    def test_merge_combines_results(self):
        """Merging combines issues, warnings, and counters."""
        result1 = ValidationResult(valid=True)
        result1.add_pass()
        result1.add_pass()
        result1.add_warning("warn1", "Warning 1")

        result2 = ValidationResult(valid=True)
        result2.add_issue("issue1", "Issue 1")
        result2.add_pass()

        result1.merge(result2)

        assert result1.valid is False  # Became invalid due to merge
        assert result1.checks_passed == 3
        assert result1.checks_failed == 1
        assert result1.checks_warned == 1
        assert len(result1.issues) == 1
        assert len(result1.warnings) == 1

    def test_to_dict_serialization(self):
        """to_dict() returns proper dictionary structure."""
        result = ValidationResult(valid=True)
        result.add_pass()
        result.add_issue("test", "Test issue")
        result.add_warning("warn", "Test warning")

        d = result.to_dict()

        assert isinstance(d, dict)
        assert d["valid"] is False
        assert isinstance(d["issues"], list)
        assert isinstance(d["warnings"], list)
        assert "summary" in d
        assert d["summary"]["checks_passed"] == 1
        assert d["summary"]["checks_failed"] == 1
        assert d["summary"]["checks_warned"] == 1


class TestExtractionValidator:
    """Integration tests for validate_all()."""

    def test_validate_all_returns_comprehensive_report(self, validator):
        """validate_all() returns a complete report with all validations."""
        report = validator.validate_all()

        assert isinstance(report, dict)
        assert "overall_valid" in report
        assert "wars" in report
        assert "fleets" in report
        assert "diplomacy" in report
        assert "resources" in report
        assert "summary" in report

    def test_validate_all_aggregates_validity(self, validator):
        """validate_all() overall_valid reflects all domain validations."""
        report = validator.validate_all()

        # Overall valid should be True only if all domains are valid
        all_valid = (
            report["wars"]["valid"]
            and report["fleets"]["valid"]
            and report["diplomacy"]["valid"]
            and report["resources"]["valid"]
        )

        assert report["overall_valid"] == all_valid


class _FleetValidatorExtractorDouble:
    def __init__(self):
        self._raw_fleets = {
            # In 4.3 the legacy station marker can coexist with the authoritative
            # mobile military ship class; the validator must follow ship_class.
            "745": {
                "ship_class": "shipclass_military",
                "station": "yes",
                "military_power": "500",
            },
            "798": {
                "ship_class": "shipclass_starbase",
                "military_power": "300",
            },
        }

    def get_fleets(self):
        return {
            "military_fleet_count": 1,
            "civilian_fleet_count": 0,
            "starbases": {"total": 1},
            "fleets": [
                {
                    "id": "745",
                    "name": "1st Fleet",
                    "military_power": 500,
                }
            ],
        }

    def get_player_empire_id(self):
        return 0

    def _find_player_country_content(self, _player_id):
        return "owned_fleets={ fleet=745 fleet=798 }"

    def _get_owned_fleet_ids(self, _country_content):
        return ["745", "798"]

    def _get_fleets_cached(self):
        return self._raw_fleets


def test_fleet_validator_uses_ship_class_before_legacy_station_marker():
    validator = object.__new__(ExtractionValidator)
    validator.extractor = _FleetValidatorExtractorDouble()
    validator.raw = "\nfleet=\n{\n\t745=\n\t{\n\t}\n\t798=\n\t{\n\t}\n}"
    validator._country_names_cache = None

    result = validator.validate_fleets()

    assert result.valid is True
    assert result.issues == []
    assert not any(warning["check"] == "count_mismatch" for warning in result.warnings)


class _MachineResourceValidatorExtractorDouble:
    def get_resources(self):
        values = {"energy": 100.0, "minerals": 50.0, "alloys": 25.0}
        return {
            "stockpiles": values,
            "monthly_income": {},
            "monthly_expenses": {},
            "net_monthly": values,
        }

    def get_empire_identity(self):
        return {"is_machine": True, "is_hive_mind": False}


def test_resource_validator_does_not_require_food_or_consumer_goods_for_machines():
    validator = object.__new__(ExtractionValidator)
    validator.extractor = _MachineResourceValidatorExtractorDouble()

    result = validator.validate_resources()

    missing = {
        warning.get("details", {}).get("resource")
        for warning in result.warnings
        if warning["check"] == "missing_essential"
    }
    assert "food" not in missing
    assert "consumer_goods" not in missing


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
