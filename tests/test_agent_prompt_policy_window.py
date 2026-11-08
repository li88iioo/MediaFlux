"""原生 SDK 帮助保留业务语义与唯一确认门；生产模型不再加载业务窗口。"""


def test_subscription_sdk_help_and_native_write_guidance():
    from app import sdk

    methods = {method["name"]: method for method in sdk.describe_methods()}
    assert {"subscriptions.list", "subscriptions.get", "subscriptions.check_updates"} <= methods.keys()
    assert "revision" in methods["subscriptions.get"]["doc"]
    assert "explicit" in methods["subscriptions.notification"]["doc"]


def test_native_playback_help_describes_scope_synonyms_and_saved_preference_contract():
    from app import sdk

    methods = {method["name"]: method for method in sdk.describe_methods()}
    assert {"library.history", "library.resume", "library.recommend"} <= methods.keys()
    assert "不能用继续观看列表代替" in methods["library.history"]["doc"]
    assert "total/has_more/complete" in methods["library.history"]["doc"]
    doc = methods["library.recommend"]["doc"]
    for rule in ("同义词用|合并", "只用于排序", "显式[]/0/False优先", "partial",
                 "历史失败会报错", "用户范围", "open_url"):
        assert rule in doc
