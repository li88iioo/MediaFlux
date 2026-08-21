((global) => {
    'use strict';

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
            if (numbering && values.numbering_mode) numbering.value = values.numbering_mode;
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

    global.MediaScrapePosition = {create};
})(window);
