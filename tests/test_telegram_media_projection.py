from __future__ import annotations

from app.modules.telegram_media_projection import (
    attach_bounded_media_details,
    build_media_detail_blocks,
)
from app.modules.telegram_organize_lifecycle import build_organize_lifecycle_event
from app.notifier import NotificationEvent, render_event, split_message


def _episode_item(**overrides) -> dict:
    item = {
        "title": "面包超人",
        "year": "1988",
        "media_type": "tv",
        "tmdb_id": "56389",
        "season": 1,
        "episode": 1,
        "season_total": 52,
        "season_present_episodes": [1],
        "source": "光鸭云盘",
        "category": "动漫/面包超人 (1988) {tmdb-56389}/Season 1",
        "version": "WEB-DL 1080p",
        "filename": "面包超人.1988.S01E01-WEB-DL.1080p.mkv",
        "size": 978_101_234,
    }
    item.update(overrides)
    return item


def test_media_projection_restores_complete_ingest_card_fields() -> None:
    block = build_media_detail_blocks([_episode_item()])[0]

    assert block.startswith("📺 面包超人 (1988) · S01")
    assert "剧集入库：" not in block
    assert "- 📺 本次更新：E01" in block
    assert "- 📊 本季进度：1 / 52 集" in block
    assert "- 🧩 缺集情况：S01E02-S01E52" in block
    assert "- ☁️ 存储来源：光鸭云盘" in block
    assert "- 🗂️ 目录分类：动漫 / 面包超人 (1988) {tmdb-56389} / Season 1" in block
    assert "- 🎛️ 规格版本：1080p · WEB-DL" in block
    assert "- 📄 文件统计：1 个 · 932.79 MB" in block
    assert "- 🎬 TMDB ID：56389" in block


def test_media_projection_marks_complete_season_and_separates_media_blocks() -> None:
    blocks = build_media_detail_blocks([
        _episode_item(season_total=1, season_present_episodes=[1]),
        _episode_item(
            title="第二部作品",
            tmdb_id="10002",
            season_total=1,
            season_present_episodes=[1],
        ),
    ])
    event = attach_bounded_media_details(
        NotificationEvent("整理完成", layout="relaxed", field_emojis=False),
        blocks,
    )
    rendered = render_event(event)

    assert "- 📊 本季进度：1 / 1 集（全） ✅" in rendered
    assert "\n\n---\n\n📺 面包超人" in rendered
    assert "\n\n---\n\n📺 第二部作品" in rendered
    assert rendered.rstrip().endswith("---")
    assert rendered.count("---") == 3


def test_partial_organize_projection_keeps_progress_without_final_missing_list() -> None:
    event = build_organize_lifecycle_event(
        {
            "total": 2,
            "moved": 1,
            "skipped": 1,
            "media_items": [_episode_item()],
        },
        source_name="光鸭云盘",
    )
    rendered = render_event(event)

    assert "- 📊 本季进度：1 / 52 集" in rendered
    assert "暂不生成最终缺集结论" in rendered
    assert "🧩 缺集情况" not in rendered


def test_media_projection_stays_editable_and_reports_omitted_groups() -> None:
    blocks = build_media_detail_blocks([
        _episode_item(
            title=f"超长媒体标题-{index}-" + ("示例" * 40),
            tmdb_id=str(100_000 + index),
            episode=index + 1,
            season_present_episodes=[index + 1],
        )
        for index in range(30)
    ])
    event = attach_bounded_media_details(
        NotificationEvent(
            "✅ 光鸭整理完成",
            fields=(("STRM", "已完成"), ("媒体库", "Jellyfin 已刷新")),
            layout="relaxed",
            field_emojis=False,
        ),
        blocks,
    )
    rendered = render_event(event)

    assert len(split_message(rendered)) == 1
    assert len(rendered) <= 3800
    assert "项媒体详情未展开" in rendered
    assert "完整记录见 Web 整理日志" in rendered
