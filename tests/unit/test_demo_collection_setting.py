"""The guided tour's demo-collection admin setting.

Pins: the setting ships empty, validation ties it to the default study's
collections, and the getter mirrors ``get_default_study``'s stored-as-is
contract.
"""

import pytest

from web_interface import admin_settings


def test_ships_unset():
    assert admin_settings.DEFAULTS["demo_collection"] == ""
    assert admin_settings.SETTING_TYPES["demo_collection"] is str


def test_validation_requires_a_default_study(monkeypatch):
    monkeypatch.setattr(admin_settings, "get_default_study", lambda: "")
    err = admin_settings.validate_setting_value("demo_collection", "c1")
    assert err and "default study" in err


def test_validation_checks_study_membership(monkeypatch):
    monkeypatch.setattr(admin_settings, "get_default_study", lambda: "study_x")
    monkeypatch.setattr(admin_settings, "demo_collection_choices", lambda: ["c1", "c2"])
    assert admin_settings.validate_setting_value("demo_collection", "c1") is None
    err = admin_settings.validate_setting_value("demo_collection", "nope")
    assert err and "not part of the default study" in err


def test_empty_value_always_valid():
    assert admin_settings.validate_setting_value("demo_collection", "") is None


def test_getter_strips_and_defaults(monkeypatch):
    monkeypatch.setattr(admin_settings, "get_setting",
                        lambda key: "  c9  " if key == "demo_collection" else None)
    assert admin_settings.get_demo_collection() == "c9"
    monkeypatch.setattr(admin_settings, "get_setting", lambda key: None)
    assert admin_settings.get_demo_collection() == ""


@pytest.mark.parametrize("default,expected", [("", []), ("study_x", ["a", "b"])])
def test_choices_come_from_the_default_study(monkeypatch, default, expected):
    monkeypatch.setattr(admin_settings, "get_default_study", lambda: default)
    if default:
        import web_interface.services.study_data as study_data
        monkeypatch.setattr(study_data, "get_study_collections",
                            lambda name: [{"collection_id": "b"}, {"collection_id": "a"}])
    assert admin_settings.demo_collection_choices() == expected
