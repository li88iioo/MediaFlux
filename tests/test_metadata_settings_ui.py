from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_metadata_settings_tmdb_test_and_stable_status_contract():
    template = (ROOT / "app/templates/settings.html").read_text(encoding="utf-8")
    script = (ROOT / "app/static/js/settings.js").read_text(encoding="utf-8")
    css = (ROOT / "app/static/css/settings-agent.css").read_text(encoding="utf-8")

    panel = template[
        template.index('id="settings-panel-metadata"'):
        template.index('id="settings-panel-discovery"')
    ]
    for marker in (
        'id="tmdbApiKeyInput"',
        'id="testTmdbBtn"',
        'id="tmdbApiUrlInput"',
        'id="tmdbConnectionState" aria-live="polite"',
        'data-preset-url="https://api.themoviedb.org/3"',
        'data-preset-url="https://api.tmdb.org/3"',
        'id="aiThresholdInput"',
        'class="ai-threshold-wrap"',
        'class="metadata-gauge-card"',
    ):
        assert marker in panel

    assert "fetch('/api/tools/tmdb/test'" in script
    assert "tmdb-connection-status is-testing" in script
    assert "tmdb-connection-status is-ok" in script
    assert "tmdb-connection-status is-error" in script
    assert "tmdb-connection-status is-idle" in script
    assert "testTmdbBtn.disabled = false;" in script
    assert ".tmdb-test-btn" in css
    assert "min-height: 42px;" in css
    assert "height: 42px;" in css
    assert ".tmdb-connection-status.is-idle { visibility: hidden; }" in css
    assert "min-height: 31px;" in css
