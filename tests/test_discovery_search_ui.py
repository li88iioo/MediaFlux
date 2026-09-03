from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "app/templates/discovery.html"
PROFILE_DIALOG = ROOT / "app/templates/_media_profile_dialog.html"
SCRIPT = ROOT / "app/static/js/discovery.js"
STYLES = ROOT / "app/static/css/main.css"


class DiscoverySearchUIContractTests(unittest.TestCase):
    def setUp(self):
        self.template = (
            TEMPLATE.read_text(encoding="utf-8")
            + PROFILE_DIALOG.read_text(encoding="utf-8")
        )
        self.script = SCRIPT.read_text(encoding="utf-8")
        self.styles = STYLES.read_text(encoding="utf-8")

    def test_template_has_stable_search_form_and_pagination_sentinel(self):
        for contract in (
            'id="discovery-search-form"',
            'id="discovery-search-query"',
            'type="search"',
            'id="discovery-search-submit"',
            'id="discovery-page-sentinel"',
            'id="discovery-load-more-row"',
            'id="discovery-load-more"',
        ):
            self.assertIn(contract, self.template)
        self.assertRegex(
            self.template,
            re.compile(
                r'<form[^>]+class="[^"]*discovery-search-form[^"]*"[^>]*>.*?'
                r'<button[^>]+type="submit"',
                re.S,
            ),
        )

    def test_toolbar_contains_compact_search_and_icon_only_actions(self):
        toolbar = re.search(r'<div class="discovery-toolbar">(?P<body>.*?)</div>\s*</section>', self.template, re.S)
        self.assertIsNotNone(toolbar)
        body = toolbar.group("body")
        self.assertRegex(body, re.compile(r'<form[^>]+id="discovery-search-form".*?<div[^>]+id="discovery-filter-region"', re.S))
        self.assertRegex(
            body,
            re.compile(
                r'<div class="discovery-search-field">.*?'
                r'<input[^>]+id="discovery-search-query".*?'
                r'<button[^>]+id="discovery-search-submit"[^>]+aria-label="搜索"',
                re.S,
            ),
        )
        for removed in ("SEARCH / MEDIA", "SHELF / 01", "放映排期按来源自动编排", ">检索<", ">刷新<"):
            self.assertNotIn(removed, self.template)
        self.assertNotIn('id="discovery-sequence"', self.template)
        self.assertIn('aria-label="等待数据源"', self.template)
        self.assertNotIn("sequence: document.getElementById", self.script)
        self.assertNotIn("elements.sequence", self.script)
        self.assertIn("elements.providerStatus.setAttribute('aria-label', summary)", self.script)
        self.assertIn("elements.providerStatus.title = summary", self.script)
        self.assertIn("elements.providerStatus.replaceChildren(icon", self.script)

    def test_filter_controls_render_as_active_compact_modules(self):
        for contract in (
            "discovery-filter-label",
            "label.classList.toggle('is-active', Boolean(select.value))",
            "elements.filters.hidden = !usable.length",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.styles,
            re.compile(r"\.discovery-filter-control\s*\{[^}]*height:\s*40px[^}]*border:\s*1px solid", re.S),
        )
        self.assertRegex(
            self.styles,
            re.compile(r"\.discovery-filter-control \.form-select\s*\{[^}]*border:\s*0[^}]*background:\s*transparent", re.S),
        )
        self.assertIn(".discovery-filter-control.is-active", self.styles)
        self.assertRegex(
            self.styles,
            re.compile(r"\.discovery-toolbar-meta\s*\{[^}]*grid-column:\s*3", re.S),
        )

    def test_search_mode_uses_existing_grid_and_discovery_search_contract(self):
        for contract in (
            "searchQuery:",
            "searchProviders:",
            "state.mode === 'search'",
            "elements.searchForm.addEventListener('submit'",
            "'/api/discovery/search'",
            "providers: state.searchProviders.join(',')",
            "loadActive({preserveContent: true}",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.script,
            re.compile(r"state\.mode\s*=\s*'search'.*?elements\.grid\.hidden\s*=\s*false", re.S),
        )

    def test_card_meta_deduplicates_year_already_present_in_release_date(self):
        self.assertIn("const year = String(item.year || '').trim();", self.script)
        self.assertIn("const releaseDate = String(item.release_date || '').trim();", self.script)
        self.assertIn(
            "const releaseYear = releaseDate.match(/^(\\d{4})(?:[-/.年]|$)/)?.[1] || '';",
            self.script,
        )
        self.assertIn("if (year && year !== releaseYear) parts.push(year);", self.script)
        self.assertNotIn("if (item.year) parts.push(String(item.year));", self.script)

    def test_media_detail_deep_link_reuses_dialog_and_cleans_url_on_close(self):
        for contract in (
            "function detailIdentityFromLocation()",
            "params.get('detail_provider')",
            "params.get('detail_type')",
            "params.get('detail_id')",
            "function detailReturnLocation()",
            "get('return_query')",
            "get('return_to')",
            "search.set('q', query)",
            "return `/search?${search.toString()}`;",
            "return ['/rss#media', '/rss#watchlist'].includes(returnTo) ? returnTo : '';",
            "function clearDetailLocation()",
            "function leaveDetailLocation()",
            "function syncDetailCloseCopy()",
            "returnTo === '/rss#media'",
            "label = '返回媒体追更'",
            "title = '关闭并返回媒体追更'",
            "returnTo === '/rss#watchlist'",
            "label = '返回收藏清单'",
            "title = '关闭并返回收藏清单'",
            "syncDetailCloseCopy()",
            "window.history.back()",
            "window.location.assign(returnTo)",
            "window.history.replaceState",
            "const initialDetail = detailIdentityFromLocation()",
            "if (initialDetail) openDetail(initialDetail, null)",
            "const resolvedItem =",
            "card?.querySelector('.discovery-map-state')",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.script,
            re.compile(
                r"function closeDetailDialog\(\).*?elements\.dialog\.close",
                re.S,
            ),
        )
        self.assertRegex(
            self.script,
            re.compile(
                r"elements\.dialog\.addEventListener\('close'.*?leaveDetailLocation\(\)",
                re.S,
            ),
        )

    def test_infinite_scroll_has_single_flight_disconnect_and_manual_fallback(self):
        for contract in (
            "new IntersectionObserver",
            "rootMargin: '600px 0px'",
            "observer.observe(elements.sentinel)",
            "function disconnectInfiniteScroll()",
            "function connectInfiniteScroll()",
            "async function loadNextPage",
            "if (state.loadingMore || state.loading || !state.hasMore)",
            "typeof window.IntersectionObserver !== 'function'",
            "elements.loadMoreRow.hidden = false",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.script,
            re.compile(r"function disconnectInfiniteScroll\(\).*?\.disconnect\(\)", re.S),
        )
        self.assertRegex(
            self.script,
            re.compile(r"function cancelActiveDiscoveryLoad\(\).*?disconnectInfiniteScroll\(\)", re.S),
        )
        self.assertRegex(
            self.script,
            re.compile(r"activateTab\(button\).*?cancelActiveDiscoveryLoad\(\)", re.S),
        )

    def test_append_deduplicates_and_exposes_local_retry_without_clearing_cards(self):
        for contract in (
            "function mergeUniqueItems",
            "new Set(",
            "itemKey(item)",
            "appendError:",
            "showPaginationControl",
            "loadActive({preserveContent: true, append: true}",
            "state.page = previousPage",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.script,
            re.compile(r"if \(append\).*?mergeUniqueItems", re.S),
        )
        self.assertNotRegex(
            self.script,
            re.compile(r"loadNextPage[\s\S]{0,1400}elements\.grid\.replaceChildren\(\)"),
        )

    def test_detail_resource_search_uses_safe_dom_and_trusted_result_ids(self):
        for contract in (
            "function resourceSearchPayload",
            "function resourceSearchLabel",
            "original_title",
            "'/api/indexers/search'",
            "'/api/indexers/download'",
            "result_id: result.result_id",
            "target",
            "site_name",
            "size_text",
            "seeders",
            "leechers",
            "downloads",
            "published_at",
        ):
            self.assertIn(contract, self.script)
        self.assertIn("element.textContent = String(text)", self.script)
        self.assertNotIn(".innerHTML", self.script)
        self.assertNotIn("insertAdjacentHTML", self.script)

    def test_resource_search_posts_structured_media_titles_without_concatenating_year(self):
        for contract in (
            "function resourceSearchPayload",
            "original_title:",
            "english_title:",
            "aliases,",
            "media_type:",
            "sort_mode: state.resourceSort",
            "method: 'POST'",
            "'Content-Type': 'application/json'",
            "JSON.stringify(searchPayload)",
        ):
            self.assertIn(contract, self.script)
        self.assertNotIn("function resourceSearchQuery", self.script)
        self.assertNotRegex(
            self.script,
            re.compile(r"INDEXER_SEARCH_PATH.*?queryString\(\{q:", re.S),
        )

    def test_resource_rows_have_independent_qb_and_guangya_busy_actions(self):
        for contract in (
            "function resourceActionButton",
            "discovery-resource-action-head",
            "discovery-resource-action-label",
            "discovery-resource-action-buttons",
            "actions.append(actionHead, actionButtons)",
            "'qBittorrent'",
            "'qb'",
            "'光鸭'",
            "'guangya'",
            "button.setAttribute('aria-busy', 'true')",
            "button.disabled = true",
            "button.disabled = false",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.script,
            re.compile(
                r"resourceActionButton\([^\n]+?'qBittorrent'[^\n]+?'qb'.*?"
                r"resourceActionButton\([^\n]+?'光鸭'[^\n]+?'guangya'",
                re.S,
            ),
        )
        self.assertNotIn("copy.append(itemStatus)", self.script)

    def test_resource_results_support_selection_batch_feedback_and_site_statuses(self):
        for contract in (
            "resourceResultsEnabled:",
            "root.dataset.resourceResultsEnabled !== 'false'",
            "selectedResourceIds:",
            "function resourceBulkToolbar",
            "function submitResourceBatch",
            "data-resource-select-all",
            "allSelected ? '清除选择' : '全选当前页'",
            "data-resource-batch-target",
            "data-resource-batch-summary",
            "'both'",
            "'/api/indexers/download/batch'",
            "site_statuses",
            "function resourceSiteStatuses",
            "discovery-resource-item-status",
            "window.appAlert",
        ):
            self.assertIn(contract, self.script)
        self.assertNotIn("data-resource-clear", self.script)

    def test_resource_site_status_exposes_query_diagnostics_without_visible_width_growth(self):
        for contract in (
            "function resourceSiteDiagnostic",
            "site?.query",
            "site?.attempts",
            "chip.title = accessibleLabel",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.script,
            re.compile(r"const accessibleLabel = .*?resourceSiteDiagnostic\(site\)", re.S),
        )

    def test_all_enabled_site_chips_filter_visible_results_without_refetching(self):
        for contract in (
            "activeResourceSiteId:",
            "function activeResourceSiteName",
            "function syncResourceHeadTitle",
            "function visibleResourceResults",
            "function renderResourceResultsList",
            "function setActiveResourceSite",
            "data-resource-site-filter",
            "button.setAttribute('aria-pressed'",
            "row.dataset.resourceSiteId",
            "createFilterButton('', `全部 ${state.resourceResults.size}`",
            "const createFilterButton = (siteId, label, accessibleLabel, status = 'success')",
            "if (status !== 'disabled' && filterSiteId)",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.script,
            re.compile(r"function setActiveResourceSite\([^)]*\)[\s\S]*?renderResourceResultsList\(\)[\s\S]*?syncResourceHeadTitle\(\)[\s\S]*?syncResourceControls\(\)", re.S),
        )
        self.assertNotRegex(
            self.script,
            re.compile(r"function setActiveResourceSite\([^)]*\)[\s\S]{0,900}(?:INDEXER_SEARCH_PATH|loadResources\()", re.S),
        )
        self.assertRegex(
            self.script,
            re.compile(r"const downloadableIds = visibleResourceResults\(\)[\s\S]*?\.filter\(isDownloadableResult\)", re.S),
        )
        self.assertRegex(
            self.styles,
            re.compile(r"\.discovery-resource-site-status\.is-filter\[aria-pressed=\"true\"\]\s*\{", re.S),
        )

    def test_resource_workbench_keeps_media_context_compact_and_layout_stable(self):
        for contract in (
            "function resourceMediaHeader",
            "discovery-resource-media-header",
            "discovery-resource-panel",
            "panel.append(mediaHeader, sites, head, list, pagination, resourceBulkToolbar(), notice)",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.discovery-resource-panel\s*\{[^}]*"
                r"display:\s*flex[^}]*"
                r"flex-direction:\s*column",
                re.S,
            ),
        )
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.discovery-resource-list\s*\{[^}]*min-height:\s*0[^}]*"
                r"grid-auto-rows:\s*max-content[^}]*overflow-y:\s*visible",
                re.S,
            ),
        )
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.discovery-resource-row\s*\{[^}]*min-height:\s*136px[^}]*"
                r"height:\s*max-content",
                re.S,
            ),
        )

    def test_resource_sorting_is_stable_and_refreshes_server_side_candidate_window(self):
        for contract in (
            "const RESOURCE_SORT_OPTIONS",
            "['published_desc', '发布时间：新到旧']",
            "['relevance_desc', '综合匹配：高到低']",
            "resourceSort: 'published_desc'",
            "['episode_desc', '季集号：高到低']",
            "function compareResourceEpisode",
            "function compareResourceResults",
            "function sortedResourceResults",
            "function setResourceSort",
            "{resort: true}",
            "排序刷新失败，现有结果已保留",
            "list.replaceChildren(...orderedRows)",
            "left.episode_end ?? left.episode",
            "right.episode_end ?? right.episode",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.script,
            re.compile(r"async function setResourceSort\([^)]*\)[\s\S]{0,900}loadResources\(", re.S),
        )
        self.assertRegex(
            self.styles,
            re.compile(r"\.discovery-resource-sort\s*\{[^}]*height:\s*36px[^}]*min-height:\s*36px", re.S),
        )

    def test_resource_mode_hides_redundant_mapping_panel_and_partial_badge(self):
        resource_branch = self.script[
            self.script.index("if (state.resourceResultsEnabled)"):
            self.script.index("const layout = node('div', 'discovery-detail-layout')")
        ]
        self.assertIn("elements.dialogBody.replaceChildren(panel);", resource_branch)
        self.assertNotIn("replaceChildren(panel, mappingPanel)", resource_branch)
        self.assertNotIn("statusBadge('SEARCHING', 'cache')", self.script)
        self.assertNotIn("badge.textContent = payload.partial ? 'PARTIAL'", self.script)

    def test_filtered_resource_rows_override_grid_display_when_hidden(self):
        self.assertIn("row.hidden = !visibleIds.has", self.script)
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.discovery-resource-row\[hidden\]\s*\{[^}]*display:\s*none",
                re.S,
            ),
        )

    def test_resource_panel_and_indexer_request_are_guarded_by_root_switch(self):
        self.assertRegex(
            self.script,
            re.compile(
                r"function renderDetail\([^)]*\).*?"
                r"if \(state\.resourceResultsEnabled\)\s*\{\s*"
                r"const panel = resourcePanel\(item,\s*detail",
                re.S,
            ),
        )
        self.assertRegex(
            self.script,
            re.compile(
                r"renderDetail\([^;]+;\s*"
                r"if \(state\.resourceResultsEnabled\)\s*\{\s*"
                r"(?:const\s+\w+\s*=\s*)?await loadResources\(",
                re.S,
            ),
        )

    def test_resource_search_retry_is_single_flight_and_rejects_stale_responses(self):
        for contract in (
            "resourceSearchController:",
            "resourceSearchRequestId:",
            "function beginResourceSearch",
            "state.resourceSearchController?.abort()",
            "resourceSearchRequestId !== state.resourceSearchRequestId",
            "function cancelResourceSearch",
        ):
            self.assertIn(contract, self.script)
        self.assertNotIn(
            "loadResources(item, detail, state.detailController?.signal",
            self.script,
        )

    def test_resource_selection_is_bounded_to_fifty_and_results_are_deduplicated(self):
        for contract in (
            "const RESOURCE_SELECTION_LIMIT = 50",
            "function setResourceSelected",
            ".slice(0, RESOURCE_SELECTION_LIMIT)",
            "最多选择 ${RESOURCE_SELECTION_LIMIT} 项",
            "function uniqueResourceResults",
            "uniqueResourceResults(extractItems(payload))",
        ):
            self.assertIn(contract, self.script)

    def test_dialog_responsive_geometry_suppresses_scrollbars_and_zeroes_bulk_bottom(self):
        for contract in (
            "body.discovery-modal-open",
            "body:has(.discovery-dialog[open])",
            "scrollbar-width: none",
            "-ms-overflow-style: none",
            ".discovery-resource-sites::-webkit-scrollbar",
        ):
            self.assertIn(contract, self.styles)
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.discovery-resource-bulk\s*\{[^}]*"
                r"position:\s*sticky[^}]*"
                r"bottom:\s*0[^}]*"
                r"flex:\s*0 0 auto[^}]*"
                r"margin-bottom:\s*0",
                re.S,
            ),
        )
        self.assertRegex(
            self.styles,
            re.compile(
                r"@media \(max-width:\s*900px\)\s*\{[\s\S]*?"
                r"\.discovery-resource-sites\s*\{[^}]*scrollbar-width:\s*none[\s\S]*?"
                r"\.discovery-resource-bulk\s*\{[^}]*bottom:\s*0[^}]*flex:\s*0 0 auto[^}]*margin-bottom:\s*0",
                re.S,
            ),
        )
        self.assertRegex(
            self.styles,
            re.compile(
                r"@media \(max-width:\s*560px\)\s*\{[\s\S]*?"
                r"\.discovery-resource-sites\s*\{[^}]*scrollbar-width:\s*none[\s\S]*?"
                r"\.discovery-resource-bulk\s*\{[^}]*bottom:\s*0[^}]*flex:\s*0 0 auto[^}]*margin-bottom:\s*0",
                re.S,
            ),
        )
        self.assertIn("document.body.classList.add('discovery-modal-open')", self.script)
        self.assertIn("document.body.classList.remove('discovery-modal-open')", self.script)


    def test_batch_items_use_local_summary_and_terminal_results_clear_selection(self):
        for contract in (
            "function normalizeResourceBatchItems",
            "function summarizeResourceBatchItems",
            "function isCompleteResourceSuccess",
            "function isTerminalResourceSelection",
            "asArray(item.failed).length > 0",
            "summary.partial > 0",
            "state.selectedResourceIds.add(item.result_id)",
        ):
            self.assertIn(contract, self.script)
        self.assertNotIn("payload.summary ||", self.script)
        self.assertRegex(
            self.script,
            re.compile(
                r"if \(isTerminalResourceSelection\(item\)\)\s*"
                r"state\.selectedResourceIds\.delete\(item\.result_id\);\s*"
                r"else state\.selectedResourceIds\.add\(item\.result_id\)",
                re.S,
            ),
        )

    def test_partial_summary_is_separate_visible_and_warns(self):
        for contract in (
            "review_required: 0",
            "else if (item.status === 'partial')",
            "summary.partial += 1",
            "else if (item.status === 'manual_review')",
            "summary.review_required += 1",
            "部分 ${summary.partial}",
            "待核对 ${summary.review_required}",
            "summary.partial > 0 || summary.review_required > 0 ? 'warning'",
        ):
            self.assertIn(contract, self.script)
        self.assertNotRegex(
            self.script,
            re.compile(r"summary\.failed \+= 1;\s*if \(item\.status === 'partial'\) summary\.partial \+= 1"),
        )

    def test_resource_terminal_failures_clear_selection_without_replay_promises(self):
        for contract in (
            "const RESOURCE_TERMINAL_STATUSES = new Set(['expired', 'request_unknown', 'manual_review'])",
            "RESOURCE_TERMINAL_STATUSES.has(item?.status)",
            "submitted.has('qb') && submitted.has('guangya')",
            "请核对下载列表/目标状态，必要时重新检索后人工处理",
            "提交状态未知，先核对下载列表；不要直接重复提交",
            "资源结果已过期，请刷新或重新检索",
            "item.error === '资源结果已过期'",
            "|| summary.review_required > 0",
            "`${countsMessage}；${RESOURCE_MANUAL_REVIEW_MESSAGE}`",
            "normalizeResourceBatchItems(resultIds, payload.items, true)",
            "resourceSubmissionState({",
            "}, false, state.resourceSubmitState.get(resultId))",
        ):
            self.assertIn(contract, self.script)
        self.assertIn("function resourceResultTerminal", self.script)
        self.assertIn("!resourceResultTerminal(resultId)", self.script)
        self.assertNotIn("资源提交失败，可保留选择后重试", self.script)
        self.assertNotIn("批量提交失败，请重试", self.script)

    def test_resource_submissions_use_independent_lifecycle_and_deferred_global_alert(self):
        for contract in (
            "resourceSubmissionRequests: new Map()",
            "pendingResourceNotifications:",
            "function beginResourceSubmission",
            "function resourceSubmissionContextActive",
            "function renderResourceNotice",
            "function flushPendingResourceNotification",
            "data-resource-notice",
            "window.setTimeout(flushPendingResourceNotification",
        ):
            self.assertIn(contract, self.script)
        batch_start = self.script.index("async function submitResourceBatch")
        single_start = self.script.index("function resourceActionButton", batch_start)
        row_start = self.script.index("function resourceRow", single_start)
        self.assertNotIn("state.detailController?.signal", self.script[batch_start:single_start])
        self.assertNotIn("state.detailController?.signal", self.script[single_start:row_start])

    def test_site_error_message_is_accessible_without_title_and_medium_track_is_stable(self):
        self.assertIn("const accessibleLabel", self.script)
        self.assertIn("node('span', 'sr-only', site.message)", self.script)
        self.assertRegex(
            self.styles,
            re.compile(
                r"@media \(max-width:\s*900px\).*?"
                r"\.discovery-resource-sites\s*\{[^}]*"
                r"flex-wrap:\s*nowrap[^}]*overflow-x:\s*auto",
                re.S,
            ),
        )

    def test_site_status_track_and_failure_filters_are_keyboard_accessible(self):
        for contract in (
            "region.tabIndex = 0",
            "region.setAttribute('aria-label'",
            "region.addEventListener('keydown'",
            "event.key === 'ArrowLeft'",
            "event.key === 'ArrowRight'",
            "region.scrollBy(",
            "chip.addEventListener('focus', syncVisibleMessage)",
            "button.type = 'button'",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.script,
            re.compile(r"if \(status !== 'disabled' && filterSiteId\)[\s\S]*?createFilterButton\(", re.S),
        )
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.discovery-resource-sites:focus-visible\s*,\s*"
                r"\.discovery-resource-site-status\.is-error:focus-visible\s*\{[^}]*outline:",
                re.S,
            ),
        )

    def test_failure_detail_and_single_site_retry_use_stable_detail_strip(self):
        for contract in (
            "discovery-resource-sites-frame",
            "discovery-resource-site-details",
            "discovery-resource-site-message",
            "detailMessages.set(chip, message)",
            "chip.addEventListener('focus', syncVisibleMessage)",
            "chip.addEventListener('blur', syncVisibleMessage)",
            "chip.addEventListener('mouseenter'",
            "chip.addEventListener('mouseleave'",
            "message.classList.toggle('is-visible'",
            "data-resource-site-retry",
            "function retryResourceSite",
            "{siteId, merge: true}",
            "searchPayload.sites = [siteId]",
            "其他源站结果已保留",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.script,
            re.compile(
                r"node\(\s*'span',\s*'',\s*"
                r"`\$\{site\.site_name \|\| '未知站点'\}："
                r"\$\{site\.message \|\| \(status === 'empty'",
                re.S,
            ),
        )
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.discovery-resource-sites-frame\s*\{[^}]*"
                r"display:\s*flex[^}]*flex-direction:\s*column",
                re.S,
            ),
        )
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.discovery-resource-site-details\s*\{[^}]*"
                r"display:\s*none",
                re.S,
            ),
        )
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.discovery-resource-site-details\.is-visible\s*\{[^}]*"
                r"display:\s*flex",
                re.S,
            ),
        )
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.discovery-resource-site-message\.is-visible\s*\{[^}]*"
                r"display:\s*flex",
                re.S,
            ),
        )

    def test_full_failure_is_not_partial_and_duplicate_is_terminal_selection(self):
        for contract in (
            "const hasFailedTargets = asArray(item.failed).length > 0",
            "item.status === 'partial' || (item.ok === true && hasFailedTargets)",
            "function isTerminalResourceSelection",
            "RESOURCE_TERMINAL_STATUSES.has(item?.status)",
        ):
            self.assertIn(contract, self.script)
        self.assertNotIn(
            "item.status === 'partial' || asArray(item.failed).length > 0",
            self.script,
        )
        self.assertRegex(
            self.script,
            re.compile(
                r"if \(isTerminalResourceSelection\(item\)\)\s*"
                r"state\.selectedResourceIds\.delete\(item\.result_id\)",
                re.S,
            ),
        )

    def test_active_detail_uses_in_dialog_notice_without_duplicate_global_queue(self):
        for contract in (
            'id="discovery-dialog-notice"',
            'id="discovery-dialog-notice-title"',
            'id="discovery-dialog-notice-text"',
            'id="discovery-dialog-notice-actions"',
            'id="discovery-dialog-notice-close"',
            'data-discovery-dialog-notice',
        ):
            self.assertIn(contract, self.template)
        self.assertNotIn('RESOURCE DISPATCH', self.template)
        for contract in (
            "const DIALOG_NOTICE_SUCCESS_MS = 4000",
            "const INDEXER_DOWNLOAD_RESUBMIT_PATH = '/api/indexers/download/resubmit'",
            "function showDialogNotification",
            "function hideDialogNotification",
            "function resourceDuplicateNotification",
            "function resubmitResourceRequest",
            "state.dialogNoticeTimer",
            "notification.type === 'success'",
            "elements.dialogNotice.hidden = false",
            "elements.dialogNoticeClose.addEventListener('click', hideDialogNotification)",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.script,
            re.compile(
                r"if \(resourceSubmissionContextActive\(submission\)\) \{.*?"
                r"showDialogNotification\(notification\);.*?"
                r"announce\(notification\.message\);.*?return;.*?\}.*?"
                r"state\.pendingResourceNotifications\.push\(notification\)",
                re.S,
            ),
        )
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.discovery-dialog-notice\s*\{[^}]*position:\s*absolute[^}]*"
                r"z-index:[^;}]+",
                re.S,
            ),
        )
        self.assertIn('.discovery-dialog-notice[hidden]', self.styles)
        self.assertIn('.discovery-dialog-notice-actions[hidden]', self.styles)
        self.assertIn('.discovery-dialog-notice-action.is-primary', self.styles)
        self.assertIn('@media (prefers-reduced-motion: reduce)', self.styles)

    def test_pending_global_notifications_are_fifo_and_flushed_serially(self):
        for contract in (
            "pendingResourceNotifications: []",
            "resourceNotificationFlushPromise:",
            "state.pendingResourceNotifications.push(notification)",
            "state.pendingResourceNotifications.shift()",
            "while (state.pendingResourceNotifications.length",
            "await window.appAlert?.(notification)",
            "async function flushPendingResourceNotifications",
        ):
            self.assertIn(contract, self.script)
        self.assertNotIn("state.pendingResourceNotification = notification", self.script)

    def test_batch_excludes_selected_results_already_submitting(self):
        for contract in (
            "function resourceResultSubmitting",
            "!resourceResultSubmitting(resultId)",
            "正在提交的资源",
            "const busyResultIds",
            "const readySelectedIds",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.script,
            re.compile(
                r"button\.disabled\s*=\s*eligibleIds\.length\s*===\s*0"
                r"\s*\|\|\s*state\.resourceBatchBusy",
                re.S,
            ),
        )

    def test_css_reserves_control_height_and_contains_mobile_resource_rows(self):
        for selector in (
            ".discovery-search-form",
            ".discovery-search-input",
            ".discovery-page-sentinel",
            ".discovery-resource-panel",
            ".discovery-resource-bulk",
            ".discovery-resource-row",
            ".discovery-resource-item-status",
            ".discovery-resource-actions",
        ):
            self.assertIn(selector, self.styles)
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.discovery-dialog\s*\{[^}]*width:\s*min\(1180px,\s*calc\(100vw - 48px\)\)",
                re.S,
            ),
        )
        self.assertRegex(
            self.styles,
            re.compile(r"\.discovery-resource-bulk\s*\{[^}]*min-height:\s*48px", re.S),
        )
        self.assertRegex(
            self.styles,
            re.compile(r"\.discovery-search-form\s*\{[^}]*min-height:\s*40px", re.S),
        )
        self.assertRegex(
            self.styles,
            re.compile(r"\.discovery-resource-row\s*\{[^}]*min-width:\s*0", re.S),
        )
        self.assertRegex(
            self.styles,
            re.compile(r"\.discovery-resource-action\s*\{[^}]*min-height:\s*38px", re.S),
        )
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.discovery-resource-copy h4\s*\{[^}]*-webkit-line-clamp:\s*2",
                re.S,
            ),
        )
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.discovery-resource-actions\s*\{[^}]*border-left:\s*1px solid",
                re.S,
            ),
        )
        discovery_start = self.styles.index("/* Discovery / 媒体探索 */")
        mobile_start = self.styles.index("@media (max-width: 560px)", discovery_start)
        mobile_end = self.styles.index("@media (prefers-reduced-motion: reduce)", mobile_start)
        self.assertGreater(mobile_end, mobile_start)
        mobile_body = self.styles[mobile_start:mobile_end]
        self.assertIn(".discovery-search-form", mobile_body)
        self.assertIn(".discovery-resource-row", mobile_body)
        self.assertIn(".discovery-resource-actions", mobile_body)
        self.assertIn("grid-template-columns: 1fr", mobile_body)
        self.assertRegex(
            mobile_body,
            re.compile(r"\.discovery-resource-list\s*\{[^}]*grid-auto-rows:\s*max-content", re.S),
        )
        self.assertRegex(
            mobile_body,
            re.compile(r"\.discovery-resource-row\s*\{[^}]*min-height:\s*104px", re.S),
        )
        self.assertNotRegex(
            mobile_body,
            re.compile(r"\.discovery-resource-row\s*\{[^}]*min-height:\s*0(?:px)?[;}]", re.S),
        )


if __name__ == "__main__":
    unittest.main()
