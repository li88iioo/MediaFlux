from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.clients.http import HttpResponse
from app.indexers.config import SUKEBEI_URL
from app.indexers.providers.nyaa import NyaaAdapter
from app.indexers.registry import IndexerRegistry
from app.indexers.result_store import IndexerResultStore
from app.indexers.service import IndexerService


class NyaaSearchSizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_sukebei_search_preserves_literal_byte_sizes(self):
        size_cells = ("0 Bytes", "1 Byte", "0.5 GiB", "")
        rows = "".join(
            f'<tr><td></td><td><a href="/view/{index}">Fixture {index}</a></td>'
            f'<td></td><td>{size}</td></tr>'
            for index, size in enumerate(size_cells, start=1)
        )
        responses = (
            HttpResponse(SUKEBEI_URL, 200, {"content-type": "text/html"}, body)
            for body in (
                b"<p>No results found</p>",
                f'<table class="torrent-list"><tbody>{rows}</tbody></table>'.encode(),
            )
        )
        http = SimpleNamespace(get=AsyncMock(side_effect=responses))
        adapter = NyaaAdapter(
            site_id="sukebei", site_name="Sukebei", base_url=SUKEBEI_URL,
            http=http, default_enabled=False,
        )
        service = IndexerService(
            registry=IndexerRegistry({"sukebei": adapter}), result_store=IndexerResultStore(),
            enabled_site_ids=("sukebei",),
        )
        self.addAsyncCleanup(service.aclose)

        result = await service.search("Fixture", site_ids=("sukebei",))

        self.assertEqual([call.kwargs["params"]["c"] for call in http.get.await_args_list], ["2_2", "0_0"])
        self.assertEqual(result.errors, [])
        self.assertEqual({item.title: (item.size_text, item.size_bytes) for item in result.items}, {
            "Fixture 1": ("0 Bytes", 0),
            "Fixture 2": ("1 Byte", 1),
            "Fixture 3": ("0.5 GiB", 536_870_912),
            "Fixture 4": (None, None),
        })
