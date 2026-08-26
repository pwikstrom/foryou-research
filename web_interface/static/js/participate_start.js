// Participation wizard (/participate/start): three self-selected steps plus
// per-platform how-to tabs. Everything is client-side: the route serves one
// static page; ?step= deep links win over the remembered step, and every
// choice is written back to localStorage so a returning visitor resumes where
// they left off. No auth calls: logged-in/out differences are rendered
// server-side in the template.
(function () {
    const STEP_KEY = 'fyp_funnel_step';
    const PLATFORM_KEY = 'fyp_funnel_platform';
    const STEPS = ['1', '2', '3'];
    const PLATFORMS = ['tiktok', 'instagram', 'youtube'];

    function stored(key, allowed, fallback) {
        try {
            const v = localStorage.getItem(key);
            return allowed.includes(v) ? v : fallback;
        } catch (e) {
            return fallback;
        }
    }

    function remember(key, value) {
        try { localStorage.setItem(key, value); } catch (e) { /* private mode */ }
    }

    function setStep(step, { scroll = false } = {}) {
        if (!STEPS.includes(step)) step = '1';
        document.querySelectorAll('.wizard-stage-pill').forEach((b) => {
            const on = b.dataset.step === step;
            b.classList.toggle('active', on);
            b.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        document.querySelectorAll('.wizard-pane').forEach((p) => {
            p.classList.toggle('show', p.dataset.pane === step);
        });
        remember(STEP_KEY, step);
        const url = new URL(window.location.href);
        url.searchParams.set('step', step);
        history.replaceState(null, '', url);
        if (scroll) window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function setPlatform(platform) {
        if (!PLATFORMS.includes(platform)) platform = 'tiktok';
        document.querySelectorAll('.wizard-platform-tab').forEach((b) => {
            b.classList.toggle('active', b.dataset.platform === platform);
        });
        PLATFORMS.forEach((p) => {
            const body = document.getElementById('wiz-howto-' + p);
            if (body) body.style.display = p === platform ? '' : 'none';
        });
        remember(PLATFORM_KEY, platform);
    }

    document.addEventListener('DOMContentLoaded', () => {
        const urlStep = new URLSearchParams(window.location.search).get('step');
        const step = STEPS.includes(urlStep)
            ? urlStep
            : stored(STEP_KEY, STEPS, '1');
        setStep(step);
        setPlatform(stored(PLATFORM_KEY, PLATFORMS, 'tiktok'));

        document.getElementById('wizard-steps').addEventListener('click', (e) => {
            const pill = e.target.closest('.wizard-stage-pill');
            if (pill) setStep(pill.dataset.step);
        });
        document.querySelectorAll('[data-goto]').forEach((el) => {
            el.addEventListener('click', (e) => {
                e.preventDefault();
                setStep(el.dataset.goto, { scroll: true });
            });
        });
        document.getElementById('wizard-platform-tabs').addEventListener('click', (e) => {
            const tab = e.target.closest('.wizard-platform-tab');
            if (tab) setPlatform(tab.dataset.platform);
        });
    });
})();
