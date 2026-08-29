// 当最大化窗口被桌面顶栏整体下移时，确保桌面端固定底栏仍位于可见区域。
(function () {
    const root = document.documentElement;
    const DESKTOP_LAYOUT_MIN_WIDTH = 901;
    const MAX_FRAME_WIDTH = 160;
    const METRIC_TOLERANCE = 2;
    let scheduledFrame = 0;

    function finiteNumber(value, fallback = 0) {
        const number = Number(value);
        return Number.isFinite(number) ? number : fallback;
    }

    function readMetrics() {
        const currentScreen = window.screen || {};
        return {
            windowTop: finiteNumber(window.screenY, finiteNumber(window.screenTop)),
            outerWidth: finiteNumber(window.outerWidth),
            outerHeight: finiteNumber(window.outerHeight),
            innerWidth: finiteNumber(window.innerWidth),
            availableTop: finiteNumber(currentScreen.availTop),
            availableHeight: finiteNumber(currentScreen.availHeight, finiteNumber(currentScreen.height)),
        };
    }

    function computeBottomInset(metrics) {
        const innerWidth = Math.max(0, finiteNumber(metrics?.innerWidth));
        const outerWidth = Math.max(0, finiteNumber(metrics?.outerWidth));
        const outerHeight = Math.max(0, finiteNumber(metrics?.outerHeight));
        const availableHeight = Math.max(0, finiteNumber(metrics?.availableHeight));
        if (innerWidth < DESKTOP_LAYOUT_MIN_WIDTH || !outerHeight || !availableHeight) return 0;

        const availableTop = finiteNumber(metrics?.availableTop);
        const windowTop = finiteNumber(metrics?.windowTop);
        const panelOffset = Math.max(0, windowTop - availableTop);
        const clippedBottom = Math.max(0, Math.ceil(windowTop + outerHeight - availableTop - availableHeight));
        const frameWidth = Math.max(0, outerWidth - innerWidth);
        const isFullHeightWindow = Math.abs(outerHeight - availableHeight) <= METRIC_TOLERANCE;
        const matchesPanelOffset = Math.abs(clippedBottom - panelOffset) <= METRIC_TOLERANCE;
        const hasRegularBrowserFrame = !outerWidth || frameWidth <= MAX_FRAME_WIDTH;

        // 只修复 GNOME/Wayland 最大化窗口被顶栏下移的场景。
        // 设备模拟、停靠 DevTools 与普通缩放窗口继续以浏览器 viewport 为准。
        if (!panelOffset || !isFullHeightWindow || !matchesPanelOffset || !hasRegularBrowserFrame) return 0;
        return clippedBottom;
    }

    function syncBottomInset() {
        const inset = computeBottomInset(readMetrics());
        const value = `${inset}px`;
        if (root.style.getPropertyValue('--mf-window-bottom-inset') !== value) {
            root.style.setProperty('--mf-window-bottom-inset', value);
        }
        return inset;
    }

    function scheduleSync() {
        if (scheduledFrame) return;
        scheduledFrame = window.requestAnimationFrame(() => {
            scheduledFrame = 0;
            syncBottomInset();
        });
    }

    window.MediaFluxViewport = Object.freeze({
        computeBottomInset,
        syncBottomInset,
    });

    syncBottomInset();
    window.addEventListener('resize', scheduleSync, {passive: true});
    window.addEventListener('focus', scheduleSync, {passive: true});
    window.visualViewport?.addEventListener('resize', scheduleSync, {passive: true});
    window.screen?.orientation?.addEventListener?.('change', scheduleSync, {passive: true});
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) scheduleSync();
    });
    document.addEventListener('pointerdown', scheduleSync, {passive: true, capture: true});
})();
