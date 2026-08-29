((global) => {
    'use strict';

    function sanitizeSearchQuery(query, {knownEpisode = null} = {}) {
        if (!query) return '';
        let text = String(query).trim();
        const technicalNoise = /^(?:1080p|720p|2160p|4k|uhd|hdr10?\+?|hdr|web-?dl|webrip|b[dr]rip|bluray|remux|h\.?264|h\.?265|x264|x265|hevc|avc|(?:hevc|avc|x26[45])[ ._-]?(?:8|10|12)bit|(?:8|10|12)bit|aac(?:[ .]?\d(?:\.\d)?)?|flac|bilibili|baha|cht|chs|jpn|big5|gb|mp4|mkv|assx?\d*|srtx?\d*)$/i;
        const episodeRange = /^(?:e?p?\s*)?\d{1,3}\s*[-~–—]\s*\d{1,3}(?:\s*(?:fin(?:al)?|complete|全集))?$/i;
        const releaseGroup = /^(?:ani|orion[-_. ]?origin|loli[-_. ]?house|h[-_. ]?enc|ktxp|lilith[-_. ]?raws|nc[-_. ]?raws|vcb[-_. ]?studio|reinforce|moozzi2|snow[-_. ]?raws|beatrice[-_. ]?raws|(?:.+[-_. ])?(?:fansub|raws?|subs?|字幕组|字幕社|压制组))$/i;
        const isBracketNoise = (content) => {
            const compact = String(content || '').replace(/\s+/g, ' ').trim();
            const tokens = compact.split(/[\s,;+/＆&._-]+/).filter(Boolean);
            const hasTechnicalToken = tokens.some((token) => technicalNoise.test(token));
            const languageTokensOnly = tokens.length > 1
                && hasTechnicalToken
                && tokens.every((token) => technicalNoise.test(token) || /^japanese$/i.test(token));
            return !compact || technicalNoise.test(compact) || episodeRange.test(compact)
                || releaseGroup.test(compact) || languageTokensOnly;
        };
        const looksLikeTitle = (content) => {
            const compact = String(content || '').replace(/\s+/g, ' ').trim();
            if (!compact || isBracketNoise(compact)) return false;
            return /[\u3400-\u9fff]/.test(compact) || compact.split(/\s+/).filter(Boolean).length >= 2;
        };

        text = text.replace(/\.[a-z0-9]{2,5}$/i, ' ');
        let leading = /^\s*(?:\[([^\]]{1,96})\]|【([^】]{1,96})】)\s*/.exec(text);
        while (leading) {
            const content = (leading[1] || leading[2] || '').trim();
            const remainder = text.slice(leading[0].length).trimStart();
            const nextBracket = /^(?:\[([^\]]{1,96})\]|【([^】]{1,96})】)/.exec(remainder);
            const compactPublisher = !/\s/.test(content) && content.length <= 24 && !/^japanese$/i.test(content);
            const remainderHasPlainTitle = remainder && !/^[\[【]/.test(remainder) && /[A-Za-z\u3400-\u9fff]/.test(remainder);
            const followedByBracketedTitle = Boolean(nextBracket && looksLikeTitle(nextBracket[1] || nextBracket[2]));
            if (isBracketNoise(content) || (compactPublisher && (remainderHasPlainTitle || followedByBracketedTitle))) {
                text = remainder;
                leading = /^\s*(?:\[([^\]]{1,96})\]|【([^】]{1,96})】)\s*/.exec(text);
                continue;
            }
            text = `${content} ${remainder}`.trim();
            break;
        }
        text = text.replace(/[（(【\[]\s*(?:仅限|僅限|限)[^）)】\]]{1,24}(?:[）)】\]]|$)/gi, ' ');
        text = text.replace(/([（(【\[])([^）)】\]]{1,96})([）)】\]])/g, (match, open, content) => (
            isBracketNoise(content) ? ' ' : match
        ));
        const episodeNumber = Number(knownEpisode);
        text = text.replace(/\s+-\s*(\d{1,3})(?=\s*(?:[（(【\[]|$))/g, (match, value) => (
            Number.isInteger(episodeNumber) && Number(value) === episodeNumber ? ' ' : match
        ));
        text = text.replace(/(?:1080p|720p|2160p|4k|uhd|hdr10?\+?|hdr|web-?dl|webrip|b[dr]rip|bluray|remux|h\.?264|h\.?265|x264|x265|hevc|avc|aac[0-9.]*|flac|bilibili|baha|cht|chs|big5|catchplay\+?|blacktv|www\.[a-z0-9.-]+)/gi, ' ');
        text = text.replace(/第[0-9一二三四五六七八九十]+[季集]/g, ' ');
        text = text.replace(/(?:S[0-9]{1,2}(?:E[0-9]{1,3})?|Season[ ._-]*[0-9]{1,2}|[0-9]{1,2}(?:st|nd|rd|th)[ ._-]*Season)/gi, ' ');
        text = text.replace(/[._\-·]+/g, ' ');
        text = text.replace(/[（(【\[]\s*[）)】\]]/g, ' ');
        return text.replace(/\s+/g, ' ').trim();
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

    global.MediaScrapePosition = {create, sanitizeSearchQuery};
})(window);
