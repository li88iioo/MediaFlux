((global) => {
    'use strict';

    function bindSearchCleaner({root, button, query, status, getInspection, onChange, onSearch}) {
        query.addEventListener('input', () => {
            if (button?.disabled) status.textContent = '搜索名称已修改，请重新精简';
        });
        button?.addEventListener('click', async () => {
            const inspection = getInspection();
            if (!inspection) return;
            const raw = query.value;
            const isCurrent = () => !root.hidden && getInspection() === inspection;
            button.disabled = true;
            status.textContent = '正在精简名称';
            try {
                const response = await fetch('/api/tools/scrape/clean-query', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({query: raw}),
                });
                const data = await response.json();
                if (!isCurrent() || query.value !== raw) return;
                if (!response.ok) throw new Error(data.error || '名称精简失败');
                const cleaned = data.query;
                if (!cleaned) {
                    status.textContent = '未识别到可保留的标题，请手动修改';
                    query.focus();
                    query.select();
                    return;
                }
                query.value = cleaned;
                onChange?.();
                const message = cleaned === raw.trim() ? '名称无需精简' : '已精简名称';
                await onSearch();
                if (isCurrent() && query.value === cleaned) {
                    status.textContent = `${message} · ${status.textContent}`;
                }
            } catch (error) {
                if (isCurrent() && query.value === raw) status.textContent = `精简失败：${error.message}`;
            } finally {
                button.disabled = false;
            }
        });
    }

    function create({root, isSingleFile, elements = {}}) {
        if (!root) throw new Error('刮削弹窗不存在');
        const fields = elements.fields || root.querySelector('[data-media-scrape-role="position-fields"]');
        const seasonField = elements.seasonField || root.querySelector('[data-media-scrape-role="season-field"]');
        const episodeField = elements.episodeField || root.querySelector('[data-media-scrape-role="episode-field"]');
        const season = elements.season || root.querySelector('[data-media-scrape-role="season"]');
        const episode = elements.episode || root.querySelector('[data-media-scrape-role="episode"]');
        const numbering = elements.numbering || root.querySelector('[data-media-scrape-role="numbering"]');
        let dirty = false;

        season?.addEventListener('input', () => { dirty = true; });
        episode?.addEventListener('input', () => { dirty = true; });

        function singleFile() {
            return typeof isSingleFile === 'function' ? Boolean(isSingleFile()) : Boolean(isSingleFile);
        }

        function sync(mediaType) {
            const isTv = mediaType === 'tv';
            const oneFile = singleFile();
            if (fields) fields.hidden = !isTv;
            if (seasonField) seasonField.hidden = !isTv;
            if (episodeField) episodeField.hidden = !isTv || !oneFile;
            fields?.classList.toggle('is-season-only', isTv && !oneFile);
        }

        function readInteger(input, minimum, maximum, label) {
            const raw = input?.value.trim() || '';
            if (!raw) return null;
            const value = Number(raw);
            if (!Number.isInteger(value) || value < minimum || value > maximum) {
                throw new Error(`${label}必须是 ${minimum}-${maximum} 的整数`);
            }
            return value;
        }

        function payload(mediaType, {singleFileRequiresDirty = true} = {}) {
            if (mediaType !== 'tv') return {};
            const oneFile = singleFile();
            const result = {};
            if (numbering) result.numbering_mode = numbering.value || 'auto';
            if (oneFile && singleFileRequiresDirty && !dirty) return result;
            const seasonValue = readInteger(season, 0, 99, '季数');
            const episodeValue = oneFile ? readInteger(episode, 1, 999, '集数') : null;
            if (seasonValue !== null) result.season = seasonValue;
            if (episodeValue !== null) {
                result.episode = episodeValue;
                if (seasonValue === null) result.season = 1;
            }
            return result;
        }

        function reset(values = {}) {
            if (season) season.value = Number.isInteger(values.season) ? String(values.season) : '';
            if (episode) episode.value = Number.isInteger(values.episode) ? String(values.episode) : '';
            if (numbering) numbering.value = values.numbering_mode || 'auto';
            dirty = false;
        }

        return {
            sync,
            payload,
            reset,
            markClean() { dirty = false; },
            isDirty() { return dirty; },
        };
    }

    global.MediaScrapePosition = {create, bindSearchCleaner};
})(window);
