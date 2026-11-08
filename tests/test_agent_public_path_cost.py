"""Public title classification: default reachable work and legacy boundaries."""
from __future__ import annotations

import asyncio
import itertools
import random
import re
from time import process_time

import pytest

from app.agent import public_safety as safety
from app.agent.domains.resources.selection import candidate_item
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.public_view import candidate_result_lines
from tests.test_resource_presentation_native import native as native
from tests.test_agent_public_text_decoding import offline as offline
from tests.test_agent_public_text_decoding import store as store

# Frozen short-input characterization oracle, never used for long cost cases.
LEGACY_RELATIVE = re.compile(
    r"(?<!\w)(?=[A-Za-z0-9._~:-]*[A-Za-z._~:-])[A-Za-z0-9._~:-]+[\\/][^\s]+"
)


@pytest.mark.parametrize("text,expected", [
    ("a/b", True), ("0a/b", True), ("123/b", False), ("_/b", True),
    ("./b", True), ("../b", True), ("~/b", True), ("-/b", True),
    (":/b", True), ("dir\\file", True), ("C:\\private\\file", True),
    ("a/b/", True), ("a/", False), ("a/ b", False), ("a/\t", False),
    ("中文a/b", False), ("éa/b", False), ("中.a/b", True),
    ("中./b", False), ("中.1/b", False), ("中..1/b", True),
    ("中_a/b", False), ("中_a~b/x", True), ("中_a~1/x", False),
    ("中_a~_1/x", True), ("中_a~-1/x", True), ("中_a~~/x", True),
    ("中_a~/x", False), ("中_a~9.x/x", True), ("中_a~9/x", False),
    ("中_a~9:0/x", True), ("中_a~9-0/x", True), ("中_a~9_0/x", True),
    ("中_a~9~0/x", True), ("a\u0301b/x", True), ("中a ! b/x", True),
    ("a.b_c", False), ("a b / c", False), ("a b /", False),
    ("MiB/s", True), ("/srv/private", True), ("12/34", False),
])
def test_relative_path_short_characterization(text, expected):
    assert bool(LEGACY_RELATIVE.search(text)) is expected
    assert bool(safety._RELATIVE_PATH_RE.search(text)) is expected


def test_relative_path_bounded_differential():
    alphabet = "a0_.~:-/\\ 中é\u0301!"
    samples = itertools.chain.from_iterable(itertools.product(alphabet, repeat=n) for n in range(5))
    rng = random.Random(602)
    random_samples = (rng.choices(alphabet + "AZ19\t\n²", k=rng.randrange(1, 81)) for _ in range(5000))
    for chars in itertools.chain(samples, random_samples):
        text = "".join(chars)
        assert bool(safety._RELATIVE_PATH_RE.search(text)) == bool(LEGACY_RELATIVE.search(text)), repr(text)


@pytest.mark.parametrize("text,public,title", [
    ("Film.Name_2026.mkv", "Film.Name_2026.mkv", "Film.Name_2026.mkv"),
    ("速度 12 MiB/s", "速度 12 MiB/s", "速度 12 MiB/s"),
    ("速度 12.5 MB/s", "速度 12.5 MB/s", "速度 12.5 MB/s"),
    ("a/b", "", ""), ("12/34", "12/34", "12/34"),
    ("标题 /srv/private/canary", "", ""), ("标题 C:\\private\\canary", "", ""),
    ("电影 https://private.invalid/canary", "", "电影 [网址]"),
    ("中a/b", "中a/b", "中a/b"), ("中.a/b", "", ""),
    ("~/", "~/", "~/"), ("~~/ 结束", "~~/ 结束", "~~/ 结束"),
    ("a! / 结束", "a! / 结束", "a! / 结束"),
])
def test_short_public_classification_contract(text, public, title):
    assert safety.sanitize_public_text(text) == public
    assert safety.sanitize_resource_title(text) == title


def long_title(kind, length):
    if kind == "plain":
        return "~" * length
    if kind == "trailing-slash":
        return ".-~" * (length // 3) + "/"
    if kind == "slash-space":
        return "~" * length + "/ 结束"
    if kind == "remote-slash":
        return "~" * length + " ! / 结束"
    if kind == "punctuation-runs":
        return ("~.-" * 25 + "! ") * (length // 77) + "/ 结束"
    if kind == "unicode-boundary":
        return "中" + "0" * length + "~/x"
    if kind == "unicode-word":
        return "中" + "0" * length + "a/x"
    if kind == "word":
        return "a" * length
    if kind == "underscores":
        return "_" * length
    if kind == "digits":
        return "1" * length + "/34"
    if kind == "dots":
        return "." * length + "/ "
    if kind == "relative-private":
        return "~" * length + "/Q2PRIVATECANARY"
    if kind == "late-private":
        return "~" * length + " /srv/private/Q2PRIVATECANARY"
    raise AssertionError(kind)


LONG_KINDS = ["plain", "trailing-slash", "slash-space", "remote-slash", "punctuation-runs",
              "unicode-boundary", "unicode-word", "word", "underscores", "digits", "dots", "relative-private", "late-private"]


@pytest.mark.parametrize("kind", LONG_KINDS)
def test_whole_title_and_consumers_have_bounded_cpu(kind):
    raw = long_title(kind, 40000)
    expected = "" if kind.endswith("private") or kind == "unicode-boundary" else raw[:300].rstrip()
    start = process_time()
    result = safety.sanitize_resource_title(raw)
    cpu = process_time() - start
    assert result == expected
    # Independent regression protection, not a product latency SLO. Includes all
    # decoding, credentials, identifier and adjacent path regexes, not one mock.
    assert cpu < 1.0, (kind, cpu)
    start = process_time()
    assert candidate_item({"title": raw}, 1)["title"] == (expected or "候选 1")
    lines = candidate_result_lines({"source_type": "resource_candidates", "items": [
        {"title": raw, "position": 1, "status": "submitted"},
    ]})
    assert lines == ["#1 · " + (raw[:160].rstrip() if expected else "资源") + "：已提交"]
    assert process_time() - start < 1.0


def test_native_rpc_long_title_cost_and_reopened_consumers(native, monkeypatch):
    from app.indexers.models import AggregatedIndexerResult, IndexerItem
    from app.agent.domains.resources.selection import current_candidate_view
    from tests.test_resource_presentation_native import context_for, display, read
    async def exercise():
        samples = []
        original_sanitize = safety.sanitize_public_text
        def observed(text, **kwargs):
            start = process_time()
            result = original_sanitize(text, **kwargs)
            samples.append((len(text), process_time() - start))
            return result
        monkeypatch.setattr(safety, 'sanitize_public_text', observed)
        long_title = '~' * 40000
        item = IndexerItem('nyaa', 'Nyaa', long_title, download_state='ready', download_kinds=('magnet',), magnet='magnet:?xt=urn:btih:'+'a'*40)
        identity = native.service.result_store.put(item)
        async def search(request, **kwargs):
            from dataclasses import replace
            return AggregatedIndexerResult(query=request.title, page=1, items=[replace(item, result_id=identity)], sites_attempted=('nyaa',), sites_succeeded=('nyaa',))
        monkeypatch.setattr(native.service, 'search_media', search)
        pipeline = native.pipeline()
        context = await context_for(native.store)
        start = process_time()
        result = await read(pipeline, context)
        response, view = await display(pipeline, context, [identity])
        assert result['items'][0]['title'] == long_title and item.title == long_title
        assert 'error' not in response and view
        cold = SQLiteKernelStore(secret_provider=lambda: 'native-resource-presentation-secret')
        restored = await current_candidate_view(state=await cold.load(owner=context.owner, session_id=context.session_id), store=cold)
        assert restored and restored['ref'] == view['ref'] and restored['items'] == view['items']
        full_scans = [cpu for length, cpu in samples if length == 40000]
        assert full_scans and max(full_scans) < 1.0, samples
        assert process_time() - start < 1.0
    asyncio.run(exercise())
