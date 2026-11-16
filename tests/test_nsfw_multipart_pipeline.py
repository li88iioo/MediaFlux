"""NSFW 数字分段贯穿识别、规格补全及版本仲裁。"""
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.media_probe import MediaProfile
from app.modules.nsfw import extract_nsfw_multipart
from app.modules.naming import movie_part_position
from app.modules.organize import Organizer, OrganizeRules
from app.modules.organize_correction import OrganizeCorrectionService
from app.modules.scraper import MatchResult, TMDBScraper
from tests.support import isolated_test_database


@pytest.mark.parametrize('suffix,expected', [
    ('_1', 1), ('_2', 2), ('_3', 3), (' 1', 1), ('-2', 2), ('.3', 3),
    ('.CD2', 2), ('_PART_3', 3), ('_2026', None), ('_1080p', None),
    ('_0', None), ('_100', None), ('', None),
])
def test_fc2_numeric_part_keeps_separator(suffix, expected):
    part = extract_nsfw_multipart(f'4k688.com@FC2-PPV-4979207{suffix}.mp4')
    assert (part.part_index if part else None) == expected


@pytest.fixture
def pipeline():
    with isolated_test_database():
        scraper = TMDBScraper()
        organizer = Organizer(client=MagicMock(), scraper=scraper)
        rules = OrganizeRules(
            nsfw_enabled=True, nsfw_exclusive=True, nsfw_category_name='NSFW',
            region_split=False, year_split=False, small_file_mb=0,
            conflict_strategy=2,
        )
        with patch('app.modules.media_probe.probe_media_profile', return_value=None):
            yield organizer, rules
        organizer.close()
        scraper.close()


def make_plans(organizer, rules, provider, *, suffix='_', title=None):
    match = MatchResult(
        title=title or 'FC2-PPV-4979207', year='2026', media_type='movie',
        provider=provider, external_id='FC2-PPV-4979207',
        confidence=1.0, status='matched', locked=True,
    )
    files = [GuangYaFile(
        f'part-{i}', f'4k688.com@FC2-PPV-4979207{suffix}{i}.mp4',
        False, i * 1000, f'etag-{i}', 'source',
    ) for i in (1, 2, 3)]
    plans = [organizer._plan_one(
        file, 'FC2-PPV-4979207', rules, match_override=replace(match),
        media_probe_cache_only=True, media_probe_cache_prefetched=True,
    ) for file in files]
    return files, plans


@pytest.mark.parametrize('provider', ['metatube', 'clean_title'])
@pytest.mark.parametrize('probe', [False, True])
def test_parts_survive_planning_probe_and_batch_conflicts(pipeline, provider, probe):
    organizer, rules = pipeline
    files, plans = make_plans(organizer, rules, provider)
    assert [p.action for p in plans] == ['move'] * 3
    if probe:
        profiles = {f.file_id: MediaProfile(resolution='1080p', video_codec='H.264') for f in files}
        with patch('app.modules.media_probe.probe_media_profiles_batch', return_value=profiles):
            organizer._probe_move_plan_profiles(
                plans, {f.file_id: f for f in files}, {}, cache_prefetched=True,
                rules=rules, automatic=False, cache_only=False, stats={},
            )
    organizer._apply_nsfw_multipart_policy(plans, rules)
    assert [p.multipart_index for p in plans] == [1, 2, 3]
    assert [movie_part_position(p.new_name)[0] for p in plans] == [1, 2, 3]
    organizer._preview_conflicts_with_inventory(
        plans, rules, lambda _plan: (None, [], {}),
        identity_history_loader=lambda _plan: set(),
    )
    assert [p.action for p in plans] == ['move'] * 3
    assert len({p.new_name for p in plans}) == 3


def test_existing_part_only_competes_with_the_same_part(pipeline):
    organizer, rules = pipeline
    _, plans = make_plans(organizer, rules, 'metatube')
    existing = GuangYaFile('archived-2', plans[1].new_name, False, 100_000)
    decisions = [organizer._resolve_variant_conflict(p, [existing], replace(rules, conflict_strategy=1))[1] for p in plans]
    assert decisions == ['new', 'skip', 'new']


def test_same_part_duplicates_still_use_priority(pipeline):
    organizer, rules = pipeline
    _, plans = make_plans(organizer, rules, 'clean_title')
    better = replace(plans[0], file_id='better-copy', size=100_000)
    candidates = [plans[0], better]
    organizer._preview_conflicts_with_inventory(
        candidates, rules, lambda _plan: (None, [], {}),
        identity_history_loader=lambda _plan: set(),
    )
    assert plans[0].action == 'skip'
    assert better.action == 'move'


def test_long_title_keeps_part_marker(pipeline):
    organizer, rules = pipeline
    _, plans = make_plans(organizer, rules, 'metatube', title='FC2-PPV-4979207 ' + '长标题' * 100)
    assert [movie_part_position(p.new_name)[0] for p in plans] == [1, 2, 3]
    assert all(len(p.new_name.encode('utf-8')) <= 240 for p in plans)


def test_unmarked_historical_file_is_not_guessed_as_a_part(pipeline):
    organizer, rules = pipeline
    _, plans = make_plans(organizer, rules, 'metatube')
    old = GuangYaFile('old-unmarked', 'FC2-PPV-4979207.2026.1080p.mp4', False, 100)
    assert all(organizer._resolve_variant_conflict(p, [old], rules)[1] == 'new' for p in plans)


def test_movie_title_part_number_is_not_a_release_part(pipeline):
    organizer, rules = pipeline
    ordinary = replace(rules, nsfw_enabled=False, nsfw_exclusive=False)
    match = MatchResult(tmdb_id='123', title='Movie Part 1', year='2026', media_type='movie',
                        provider='tmdb', confidence=1.0, status='matched', locked=True)
    plans = [organizer._plan_one(
        GuangYaFile(str(i), name, False, 1000, 'etag', 'source'), '', ordinary,
        match_override=replace(match), media_probe_cache_only=True, media_probe_cache_prefetched=True,
    ) for i, name in enumerate(('Movie.Part.1.2026.mkv', 'Movie.Part.1.2026.CD2.mkv'))]
    assert [plan.multipart_index for plan in plans] == [None, 2]
    assert plans[0].new_name == 'Movie Part 1.2026.mkv'
    assert plans[1].new_name == 'Movie Part 1.2026.CD2.mkv'


@pytest.mark.parametrize('provider', ['clean_title', 'tmdb'])
def test_log_reorganize_recovers_part_from_original_name(pipeline, provider):
    organizer, rules = pipeline
    original = ('4k688.com@FC2-PPV-4970405_2.mp4' if provider == 'clean_title'
                else 'FC2-PPV-4970405.CD2.mp4')
    current = 'FC2-PPV-4970405.2026.1080p.mp4'
    if provider == 'tmdb':
        rules = replace(rules, nsfw_enabled=False, nsfw_exclusive=False)
    log_id = db.add_organize_log(
        'guangya', 'FC2-PPV-4970405', 'NSFW/FC2-PPV-4970405/' + current,
        'video', 'success', '123' if provider == 'tmdb' else '', provider=provider, external_id='FC2-PPV-4970405',
        media_type='movie', title='FC2-PPV-4970405', legacy_incomplete=False,
        source_dir_id='source', original_name=original, original_parent_id='source',
        current_name=current, current_parent_id='archive',
    )
    db.add_organize_log_items(log_id, [{
        'file_id': 'video', 'role': 'video', 'original_name': original,
        'original_parent_id': 'source', 'current_name': current,
        'current_parent_id': 'archive', 'size': 100, 'etag': 'etag', 'status': 'success',
    }])
    service = OrganizeCorrectionService(client=organizer.client, scraper=organizer.scraper)
    organizer.client.file_info.return_value = GuangYaFile('video', current, False, 100, 'etag', 'archive')
    with (patch('app.modules.organize_correction.OrganizeRules.from_config', return_value=rules),
          patch.object(organizer.scraper, 'match_from_tmdb', return_value=MatchResult(
              tmdb_id='123', title='FC2-PPV-4970405', year='2026', media_type='movie',
              provider='tmdb', confidence=1.0, status='matched', locked=True))):
        preview = service.preview_reorganize(
            log_id, '123' if provider == 'tmdb' else '', 'movie', provider=provider, external_id='FC2-PPV-4970405',
        )
    assert movie_part_position(preview['file_name'])[0] == 2
    organizer.client.move.assert_not_called()
    organizer.client.rename.assert_not_called()
    get_organize_log_result = db.get_organize_log(log_id)
    assert get_organize_log_result is not None
    assert get_organize_log_result['current_name'] == current
