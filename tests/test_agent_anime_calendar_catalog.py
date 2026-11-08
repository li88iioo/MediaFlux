"""元数据及演职员旧 live spec 退役，保留原生方法与其他独立业务工具。"""
from app import sdk
from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.public_safety import public_tool_label


RETIRED = {"discovery.search", "discovery.detail", "discovery.recommend",
           "discovery.anime_calendar", "bangumi.calendar", "discovery.credits", "discovery.person_filmography", "discovery.lookup_rating",
           "discovery.mapping_candidates", "discovery.confirm_mapping"}


def test_metadata_and_mapping_specs_are_retired_in_favor_of_native_sdk():
    names = {tool.name for tool in build_tool_catalog().visible()}
    assert names.isdisjoint(RETIRED)
    assert {"media.resolve", "media.confirm_mapping"} <= sdk.METHODS.keys()
    assert "media.confirm_mapping" in sdk.WRITE_METHODS and "media.resolve" not in sdk.WRITE_METHODS
    assert {"media.search", "media.detail", "media.catalog", "media.calendar", "media.people", "media.credits", "media.filmography", "media.rating"} <= sdk.METHODS.keys()
    assert not any(name in sdk.WRITE_METHODS for name in ("media.search", "media.detail", "media.catalog", "media.calendar", "media.people", "media.credits", "media.filmography", "media.rating"))


def test_old_history_labels_remain_readable():
    assert public_tool_label("discovery.anime_calendar") != "discovery.anime_calendar"
    assert public_tool_label("bangumi.calendar") != "bangumi.calendar"
