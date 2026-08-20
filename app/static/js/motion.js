/**
 * MediaFlux 全局动效与微交互基础设施 (MFAnim)
 * 基于离线纯净版 GSAP 3.12.5 构建，零外网依赖。
 * 遵循无障碍降级与极简高性能原则。
 */
(function () {
    'use strict';

    // 检查用户是否偏好减弱动画 (Accessibility) 或处于省流模式
    function shouldReduceMotion() {
        if (typeof window === 'undefined') return true;
        const prefersReduced = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
        const saveData = navigator?.connection?.saveData === true;
        return Boolean(prefersReduced || saveData);
    }

    // 屏幕断点检测 (<= 768px 为移动端)
    function isMobile() {
        if (typeof window === 'undefined') return false;
        return window.innerWidth <= 768;
    }

    // 内部数值提取助手
    function parseNumericValue(val) {
        if (typeof val === 'number') return val;
        if (!val) return 0;
        const clean = String(val).replace(/[^\d.-]/g, '');
        const parsed = parseFloat(clean);
        return isNaN(parsed) ? 0 : parsed;
    }

    const MFAnim = {
        /**
         * 状态与降级检测
         */
        reducedMotion: shouldReduceMotion(),
        isMobile: isMobile,

        /**
         * 数字平滑累加动画 (CountUp)
         * @param {string|HTMLElement} target - 目标 DOM 节点或选择器
         * @param {number} endValue - 最终目标数值
         * @param {object} options - 配置项 (duration, ease, formatter, onComplete)
         */
        countUp(target, endValue, options = {}) {
            const el = typeof target === 'string' ? document.querySelector(target) : target;
            if (!el) return;

            const targetVal = parseNumericValue(endValue);
            const initialVal = options.startValue !== undefined
                ? parseNumericValue(options.startValue)
                : parseNumericValue(el.dataset.countValue || el.textContent);

            const formatter = options.formatter || ((v) => Math.round(v).toLocaleString('zh-CN'));

            // 降级处理
            if (shouldReduceMotion() || !window.gsap) {
                el.textContent = formatter(targetVal);
                el.dataset.countValue = String(targetVal);
                options.onComplete?.();
                return;
            }

            // 使用 GSAP 代理对象进行浮点步进插值
            const obj = { val: initialVal };
            const duration = options.duration !== undefined ? options.duration : 0.6;
            const ease = options.ease || 'power2.out';

            window.gsap.to(obj, {
                val: targetVal,
                duration: duration,
                ease: ease,
                onUpdate: () => {
                    el.textContent = formatter(obj.val);
                },
                onComplete: () => {
                    el.textContent = formatter(targetVal);
                    el.dataset.countValue = String(targetVal);
                    options.onComplete?.();
                }
            });
        },

        /**
         * 元素分批交错淡入 (Stagger In - 适用于海报流、卡片列表)
         * @param {string|Array|NodeList} targets
         * @param {object} options
         */
        staggerIn(targets, options = {}) {
            const list = typeof targets === 'string' ? document.querySelectorAll(targets) : targets;
            if (!list || list.length === 0) return;
            // 页面卡片、列表与面板保持静止；保留 API 兼容性与完成回调。
            Array.from(list).forEach((el) => {
                el.style.opacity = '1';
                el.style.transform = 'none';
            });
            options.onComplete?.();
        },

        /**
         * 物理弹性抖动 (Shake - 适用于密码错误、表单校验失败、警告卡片)
         * @param {string|HTMLElement} target
         * @param {object} options
         */
        shake(target, options = {}) {
            const el = typeof target === 'string' ? document.querySelector(target) : target;
            if (!el) return;

            if (shouldReduceMotion() || !window.gsap) {
                el.classList.add('is-shaking');
                setTimeout(() => el.classList.remove('is-shaking'), 300);
                options.onComplete?.();
                return;
            }

            const intensity = options.intensity || 8;
            const duration = options.duration || 0.35;

            const tl = window.gsap.timeline({
                onComplete: () => {
                    window.gsap.set(el, { clearProps: 'x' });
                    options.onComplete?.();
                }
            });

            tl.to(el, { x: -intensity, duration: duration * 0.18, ease: 'power1.inOut' })
              .to(el, { x: intensity, duration: duration * 0.18, ease: 'power1.inOut' })
              .to(el, { x: -intensity * 0.5, duration: duration * 0.18, ease: 'power1.inOut' })
              .to(el, { x: intensity * 0.5, duration: duration * 0.18, ease: 'power1.inOut' })
              .to(el, { x: 0, duration: duration * 0.28, ease: 'power1.out' });
        },

        /**
         * 物理消除与高度平滑折叠 (Slide-out & Collapse - 适用于单项删除、清理)
         * 先向左滑动淡出，再平滑收缩高度至 0，最后从 DOM 中移除
         * @param {HTMLElement} row - 待删除的行或卡片元素
         * @param {Function} onComplete - 动画完成后的回调 (DOM 移除后调用)
         * @param {object} options
         */
        slideOutAndCollapse(row, onComplete, options = {}) {
            if (!row) {
                onComplete?.();
                return;
            }

            if (shouldReduceMotion() || !window.gsap) {
                row.remove();
                onComplete?.();
                return;
            }

            const xOffset = options.x !== undefined ? options.x : -28;
            const tl = window.gsap.timeline({
                onComplete: () => {
                    row.remove();
                    onComplete?.();
                }
            });

            // 1. 先左滑淡出
            tl.to(row, {
                x: xOffset,
                opacity: 0,
                duration: 0.18,
                ease: 'power2.in'
            });

            // 2. 再平滑折叠高度与边距
            tl.to(row, {
                height: 0,
                minHeight: 0,
                maxHeight: 0,
                paddingTop: 0,
                paddingBottom: 0,
                marginTop: 0,
                marginBottom: 0,
                borderWidth: 0,
                duration: 0.2,
                ease: 'power2.out'
            });
        },

        /**
         * 元素微弹浮现 (Pop In - 适用于新消息、Toast、Badge)
         */
        popIn(target, options = {}) {
            const el = typeof target === 'string' ? document.querySelector(target) : target;
            if (!el) return;

            if (shouldReduceMotion() || !window.gsap) {
                el.style.opacity = '1';
                el.style.transform = 'none';
                options.onComplete?.();
                return;
            }

            window.gsap.fromTo(
                el,
                {
                    opacity: 0,
                    scale: options.scale || 0.94,
                    y: options.y !== undefined ? options.y : 8
                },
                {
                    opacity: 1,
                    scale: 1,
                    y: 0,
                    duration: options.duration || 0.22,
                    ease: options.ease || 'back.out(1.4)',
                    clearProps: 'transform,opacity',
                    onComplete: options.onComplete
                }
            );
        },

        /**
         * 元素平滑淡出收缩 (Pop Out)
         */
        popOut(target, options = {}) {
            const el = typeof target === 'string' ? document.querySelector(target) : target;
            if (!el) return;

            if (shouldReduceMotion() || !window.gsap) {
                el.remove();
                options.onComplete?.();
                return;
            }

            window.gsap.to(el, {
                opacity: 0,
                scale: 0.94,
                y: options.y !== undefined ? options.y : -6,
                duration: options.duration || 0.18,
                ease: 'power2.in',
                onComplete: () => {
                    el.remove();
                    options.onComplete?.();
                }
            });
        },

        /**
         * 交叉淡化过渡 (Crossfade - 适用于面板或状态切换)
         * @param {HTMLElement} oldEl
         * @param {HTMLElement} newEl
         * @param {object} options
         */
        crossfade(oldEl, newEl, options = {}) {
            if (!newEl) return;
            // 页面面板直接切换，避免卡片随容器淡入淡出；数字 countUp 独立保留。
            if (oldEl) oldEl.hidden = true;
            newEl.hidden = false;
            newEl.style.opacity = '';
            options.onComplete?.();
        }
    };

    window.MFAnim = MFAnim;
})();
