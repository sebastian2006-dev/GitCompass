// =============================================================
// app.js — GitCompass Frontend
// Landing animations, auth gating, live indexing pipeline,
// syntax-highlighted chat, Supabase sync
// =============================================================

const API_BASE_URL = 'https://gitcompass-api.onrender.com/api';

// ---------------------------------------------------------------------
// State
// ---------------------------------------------------------------------

const ingestedRepos = new Map(); // repo_url -> { numChunks, numFiles }
const pipelines = new Map();     // chatId -> pipeline state (transient, not persisted)
let chats = [];
let activeChatId = null;
let chatCounter = 0;
let currentUser = null;
let isSignUpMode = true;  // auth form toggle
let saveDebounceTimer = null;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------

// Screens
const landingPage = document.getElementById('landing-page');
const authScreen = document.getElementById('auth-screen');
const appShell = document.getElementById('app-shell');

// Landing
const landingGetStarted = document.getElementById('landing-get-started');
const landingSignIn = document.getElementById('landing-sign-in');

// Auth
const authCard = document.getElementById('auth-card');
const authBackBtn = document.getElementById('auth-back-btn');
const authTitle = document.getElementById('auth-title');
const authSubtitle = document.getElementById('auth-subtitle');
const authForm = document.getElementById('auth-form');
const authUsername = document.getElementById('auth-username');
const authPassword = document.getElementById('auth-password');
const authError = document.getElementById('auth-error');
const authSubmitBtn = document.getElementById('auth-submit-btn');
const authSubmitText = document.getElementById('auth-submit-text');
const authSubmitSpinner = document.getElementById('auth-submit-spinner');
const authToggleText = document.getElementById('auth-toggle-text');
const authToggleBtn = document.getElementById('auth-toggle-btn');

// App
const chatListEl = document.getElementById('chat-list');
const newChatBtn = document.getElementById('new-chat-btn');
const emptyNewChatBtn = document.getElementById('empty-new-chat-btn');
const emptyStateEl = document.getElementById('empty-state');
const ingestViewEl = document.getElementById('ingest-view');
const chatViewEl = document.getElementById('chat-view');
const repoUrlInput = document.getElementById('repo-url');
const ingestBtn = document.getElementById('ingest-btn');
const ingestStatus = document.getElementById('ingest-status');
const knownReposEl = document.getElementById('known-repos');
const knownReposListEl = document.getElementById('known-repos-list');
const chatViewRepoEl = document.getElementById('chat-view-repo');
const messagesEl = document.getElementById('messages');
const queryInput = document.getElementById('query-input');
const queryBtn = document.getElementById('query-btn');

// Pipeline (indexing progress, docked beside the chat)
const pipelineSideEl = document.getElementById('pipeline-side');
const pipelinePanelEl = document.getElementById('pipeline-panel');
const pipelineReopenBtn = document.getElementById('pipeline-reopen-btn');
const pipelineReopenLabel = document.getElementById('pipeline-reopen-label');

// Sidebar user
const userAvatarEl = document.getElementById('user-avatar');
const userNameEl = document.getElementById('user-name');

// Settings
const settingsBtn = document.getElementById('settings-btn');
const settingsOverlay = document.getElementById('settings-overlay');
const settingsBackdrop = document.getElementById('settings-backdrop');
const settingsCloseBtn = document.getElementById('settings-close-btn');
const settingsAvatarEl = document.getElementById('settings-avatar');
const settingsUsernameEl = document.getElementById('settings-username');
const settingsEmailEl = document.getElementById('settings-email');
const settingsSwitchBtn = document.getElementById('settings-switch-btn');
const settingsSignoutBtn = document.getElementById('settings-signout-btn');
const settingsDeleteBtn = document.getElementById('settings-delete-btn');

// Confirm dialog
const confirmDialog = document.getElementById('confirm-dialog');
const confirmTitle = document.getElementById('confirm-title');
const confirmMessage = document.getElementById('confirm-message');
const confirmCancel = document.getElementById('confirm-cancel');
const confirmOk = document.getElementById('confirm-ok');

// ---------------------------------------------------------------------
// Screen Management
// ---------------------------------------------------------------------

function showScreen(screenName) {
    landingPage.classList.remove('active');
    authScreen.classList.remove('active');
    appShell.classList.remove('active');

    switch (screenName) {
        case 'landing':
            landingPage.classList.add('active');
            break;
        case 'auth':
            authScreen.classList.add('active');
            setTimeout(() => authCard.classList.add('visible'), 50);
            break;
        case 'app':
            appShell.classList.add('active');
            break;
    }
}

function showView(view) {
    emptyStateEl.classList.remove('visible');
    ingestViewEl.classList.remove('visible');
    chatViewEl.classList.remove('visible');

    if (view === 'empty') emptyStateEl.classList.add('visible');
    if (view === 'ingest') ingestViewEl.classList.add('visible');
    if (view === 'chat') chatViewEl.classList.add('visible');
}

// ---------------------------------------------------------------------
// Compass drag-to-spin
// ---------------------------------------------------------------------
// Grabbing the compass and dragging spins the whole dial (housing,
// bezel, needle together — like turning a real compass over in your
// hand) by tracking the pointer's angle around the center as it
// moves. On release it keeps coasting with its last angular velocity
// and eases to a stop, then the ambient idle animations (slow bezel
// turn, needle wobble) resume from wherever it settled.

function initCompassDrag() {
    const svg = document.getElementById('compass-svg');
    const hitArea = document.querySelector('.compass-interactive');
    if (!svg || !hitArea || svg.dataset.dragBound) return;
    svg.dataset.dragBound = 'true';

    let dragging = false;
    let centerX = 0, centerY = 0;
    let startAngle = 0;
    let baseRotation = 0;
    let lastAngle = 0;
    let lastTime = 0;
    let angularVelocity = 0; // deg/ms, for release momentum
    let coastAnim = null;

    const currentRotation = () => {
        const match = /rotate\(([-\d.]+)deg\)/.exec(svg.style.transform || '');
        return match ? parseFloat(match[1]) : 0;
    };

    const angleAt = (clientX, clientY) =>
        Math.atan2(clientY - centerY, clientX - centerX) * (180 / Math.PI);

    const onPointerDown = (e) => {
        if (coastAnim) { coastAnim.pause(); coastAnim = null; }
        stopCompassAmbient();

        const rect = svg.getBoundingClientRect();
        centerX = rect.left + rect.width / 2;
        centerY = rect.top + rect.height / 2;

        dragging = true;
        baseRotation = currentRotation();
        startAngle = angleAt(e.clientX, e.clientY);
        lastAngle = startAngle;
        lastTime = performance.now();
        angularVelocity = 0;

        hitArea.setPointerCapture(e.pointerId);
    };

    const onPointerMove = (e) => {
        if (!dragging) return;
        const now = performance.now();
        const angle = angleAt(e.clientX, e.clientY);
        const delta = angle - startAngle;
        svg.style.transform = `rotate(${baseRotation + delta}deg)`;

        const dt = now - lastTime;
        if (dt > 0) {
            let step = angle - lastAngle;
            // normalize across the -180/180 wrap so velocity doesn't spike
            if (step > 180) step -= 360;
            if (step < -180) step += 360;
            angularVelocity = step / dt;
        }
        lastAngle = angle;
        lastTime = now;
    };

    const onPointerUp = (e) => {
        if (!dragging) return;
        dragging = false;
        try { hitArea.releasePointerCapture(e.pointerId); } catch (_) { }

        const startRotation = currentRotation();
        // Coast: keep spinning with the release velocity, decaying to a
        // stop. Purely visual (transform), so it never fights the
        // ambient bezel-turn animation below, which restarts fresh once
        // this settles.
        const velocityDegPerSec = angularVelocity * 1000;
        const coastDistance = velocityDegPerSec * 0.6; // ~0.6s of decaying travel
        const target = startRotation + coastDistance;

        coastAnim = anime.animate(svg, {
            transform: [`rotate(${startRotation}deg)`, `rotate(${target}deg)`],
            duration: Math.min(1400, Math.max(300, Math.abs(coastDistance) * 12)),
            ease: 'outQuint',
            onComplete: () => {
                coastAnim = null;
                startCompassAmbient();
            },
        });
    };

    hitArea.addEventListener('pointerdown', onPointerDown);
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    window.addEventListener('pointercancel', onPointerUp);
}

function playLandingAnimations() {
    animateCompassBg();
    initCompassDrag();

    anime.animate('.landing-logo', {
        opacity: [0, 1],
        translateY: [-8, 0],
        duration: 600,
        delay: 0,
        ease: 'outExpo',
    });

    anime.animate('.title-line', {
        opacity: [0, 1],
        translateY: [30, 0],
        delay: anime.stagger(200, { start: 300 }),
        duration: 800,
        ease: 'outExpo',
    });

    anime.animate('.landing-badge', {
        opacity: [0, 1],
        translateY: [-10, 0],
        duration: 600,
        delay: 100,
        ease: 'outExpo',
    });

    anime.animate('.landing-subtitle', {
        opacity: [0, 1],
        translateY: [15, 0],
        duration: 700,
        delay: 800,
        ease: 'outExpo',
    });

    anime.animate('.landing-actions', {
        opacity: [0, 1],
        translateY: [15, 0],
        duration: 700,
        delay: 1000,
        ease: 'outExpo',
    });

    anime.animate('.landing-features .feature-item', {
        opacity: [0, 1],
        translateY: [15, 0],
        delay: anime.stagger(90, { start: 1150 }),
        duration: 600,
        ease: 'outExpo',
    });
}

// ---------------------------------------------------------------------
// Signature landing visual: an embossed compass rose
// ---------------------------------------------------------------------
// The bezel (tick ring + N/E/S/W labels) turns slowly and endlessly,
// as if it's always re-orienting itself. The needle spins in on load
// like it's calibrating, settles pointing "north", then idles with a
// small, realistic wobble. The whole dial is also draggable — grabbing
// it and dragging spins it by hand (see initCompassDrag). Built once,
// then replayed (not rebuilt) on repeat visits to the landing screen.

const prefersReducedMotion = () =>
    window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function buildCompassBg() {
    const bezel = document.getElementById('compass-bezel');
    if (!bezel || bezel.dataset.built) return;

    const CX = 500, CY = 500;

    // Tick ring — 60 ticks around the bezel, every 6°, with a longer
    // "major" tick every 30° (12 points, like hour marks on a clock).
    const RADIUS_OUT = 260;
    for (let i = 0; i < 60; i++) {
        const angle = (i / 60) * Math.PI * 2 - Math.PI / 2;
        const major = i % 5 === 0;
        const len = major ? 26 : 12;
        const rInner = RADIUS_OUT - len;
        const x1 = CX + Math.cos(angle) * rInner;
        const y1 = CY + Math.sin(angle) * rInner;
        const x2 = CX + Math.cos(angle) * RADIUS_OUT;
        const y2 = CY + Math.sin(angle) * RADIUS_OUT;
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', x1.toFixed(1));
        line.setAttribute('y1', y1.toFixed(1));
        line.setAttribute('x2', x2.toFixed(1));
        line.setAttribute('y2', y2.toFixed(1));
        line.setAttribute('class', 'compass-tick' + (major ? ' major' : ''));
        bezel.appendChild(line);
    }

    // Cardinal labels
    const cardinals = [
        { label: 'N', x: CX, y: CY - RADIUS_OUT + 42 },
        { label: 'E', x: CX + RADIUS_OUT - 42, y: CY },
        { label: 'S', x: CX, y: CY + RADIUS_OUT - 42 },
        { label: 'W', x: CX - RADIUS_OUT + 42, y: CY },
    ];
    cardinals.forEach(({ label, x, y }) => {
        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('x', x);
        text.setAttribute('y', y);
        text.setAttribute('class', 'compass-cardinal');
        text.textContent = label;
        bezel.appendChild(text);
    });

    bezel.dataset.built = 'true';
}

function animateCompassBg() {
    buildCompassBg();

    const housing = document.querySelector('.compass-housing');
    const rim = document.querySelector('.compass-housing-rim');
    const dial = document.querySelector('.compass-dial');
    const ticks = document.querySelectorAll('.compass-tick');
    const cardinals = document.querySelectorAll('.compass-cardinal');
    const needleParts = document.querySelectorAll('.needle-north, .needle-south, .needle-hub, .needle-hub-core');
    const bezel = document.getElementById('compass-bezel');
    const needle = document.getElementById('compass-needle');
    if (!housing) return;

    stopCompassAmbient();
    [housing, rim, dial, ...ticks, ...cardinals, ...needleParts]
        .forEach((el) => el && el.removeAttribute('style'));
    if (bezel) bezel.style.rotate = '0deg';
    if (needle) needle.style.rotate = '-140deg';

    const reduced = prefersReducedMotion();

    anime.animate([housing, rim, dial], {
        opacity: [0, 1],
        scale: [0.92, 1],
        duration: 1300,
        ease: 'outExpo',
        onComplete: startCompassAmbient,
    });

    anime.animate(ticks, {
        opacity: [0, 1],
        delay: anime.stagger(8, { start: 150 }),
        duration: 500,
        ease: 'outSine',
    });

    anime.animate(cardinals, {
        opacity: [0, 1],
        delay: anime.stagger(80, { start: 500 }),
        duration: 500,
        ease: 'outExpo',
    });

    // Needle "calibrates": spins through a few full turns, then eases
    // into rest pointing north.
    anime.animate(needleParts, {
        opacity: [0, 1],
        duration: 400,
        delay: 250,
    });

    if (needle) {
        anime.animate(needle, {
            rotate: reduced ? '0deg' : ['-140deg', '740deg', '360deg'],
            duration: reduced ? 1 : 2400,
            delay: 250,
            ease: 'outElastic(1, .65)',
            onComplete: () => {
                if (reduced) return;
                window._compassNeedleWobble = anime.animate(needle, {
                    rotate: ['360deg', '356deg', '364deg', '360deg'],
                    duration: 7000,
                    loop: true,
                    ease: 'inOutSine',
                });
            },
        });
    }
}

function startCompassAmbient() {
    if (prefersReducedMotion()) return;
    window._compassAmbientAnims = window._compassAmbientAnims || [];

    const bezel = document.getElementById('compass-bezel');
    if (bezel) {
        window._compassAmbientAnims.push(
            anime.animate(bezel, {
                rotate: '360deg',
                duration: 200000,
                loop: true,
                ease: 'linear',
            })
        );
    }
}

function stopCompassAmbient() {
    if (window._compassAmbientAnims) {
        window._compassAmbientAnims.forEach((a) => a && a.pause && a.pause());
        window._compassAmbientAnims = [];
    }
    if (window._compassNeedleWobble) {
        window._compassNeedleWobble.pause && window._compassNeedleWobble.pause();
        window._compassNeedleWobble = null;
    }
}

// ---------------------------------------------------------------------
// Auth UI
// ---------------------------------------------------------------------

function setAuthMode(isSignUp) {
    isSignUpMode = isSignUp;
    authTitle.textContent = isSignUp ? 'Create Account' : 'Welcome Back';
    authSubtitle.textContent = isSignUp
        ? 'Enter a username and password to get started.'
        : 'Sign in to access your conversations.';
    authSubmitText.textContent = isSignUp ? 'Create Account' : 'Sign In';
    authToggleText.textContent = isSignUp ? 'Already have an account?' : 'Don\'t have an account?';
    authToggleBtn.textContent = isSignUp ? 'Sign In' : 'Sign Up';
    authPassword.autocomplete = isSignUp ? 'new-password' : 'current-password';
    authError.hidden = true;
    authError.textContent = '';
}

function setAuthBusy(busy) {
    authSubmitBtn.disabled = busy;
    authUsername.disabled = busy;
    authPassword.disabled = busy;
    authSubmitText.hidden = busy;
    authSubmitSpinner.hidden = !busy;
}

async function handleAuthSubmit(e) {
    e.preventDefault();

    const username = authUsername.value.trim();
    const password = authPassword.value;

    if (!username || !password) {
        authError.textContent = 'Please fill in all fields.';
        authError.hidden = false;
        return;
    }

    if (password.length < 6) {
        authError.textContent = 'Password must be at least 6 characters.';
        authError.hidden = false;
        return;
    }

    setAuthBusy(true);
    authError.hidden = true;

    try {
        if (isSignUpMode) {
            await signUp(username, password);
        } else {
            await signIn(username, password);
        }

        const session = await getSession();
        if (session) {
            currentUser = session.user;
            await enterApp();
        }
    } catch (err) {
        let msg = err.message || 'Authentication failed.';
        if (msg.includes('User already registered')) {
            msg = 'Username already taken. Try a different one.';
        } else if (msg.includes('Invalid login credentials')) {
            msg = 'Wrong username or password.';
        }
        authError.textContent = msg;
        authError.hidden = false;
    } finally {
        setAuthBusy(false);
    }
}

// ---------------------------------------------------------------------
// App Entry (after auth)
// ---------------------------------------------------------------------

async function enterApp() {
    stopCompassAmbient();

    updateUserDisplay();

    try {
        const savedChats = await loadConversations();
        chats = savedChats;
        chatCounter = chats.length;
    } catch (err) {
        console.error('Failed to load conversations:', err);
        chats = [];
    }

    showScreen('app');
    renderChatList();

    if (chats.length > 0) {
        switchToChat(chats[0].id);
    } else {
        showView('empty');
    }
}

function updateUserDisplay() {
    if (!currentUser) return;

    const username = currentUser.user_metadata?.username
        || emailToUsername(currentUser.email)
        || 'User';
    const initial = username.charAt(0).toUpperCase();

    userAvatarEl.textContent = initial;
    userNameEl.textContent = username;

    settingsAvatarEl.textContent = initial;
    settingsUsernameEl.textContent = username;
    settingsEmailEl.textContent = currentUser.email;
}

// ---------------------------------------------------------------------
// Repo label helper
// ---------------------------------------------------------------------

function repoLabel(repoUrl) {
    try {
        const cleaned = repoUrl.replace(/\.git$/, '').replace(/\/$/, '');
        const parts = cleaned.split('/');
        return parts.slice(-2).join('/');
    } catch {
        return repoUrl;
    }
}

// ---------------------------------------------------------------------
// Sidebar rendering
// ---------------------------------------------------------------------

function renderChatList() {
    chatListEl.innerHTML = '';

    chats.forEach(chat => {
        const entry = document.createElement('div');
        entry.className = 'chat-entry' + (chat.id === activeChatId ? ' active' : '');
        entry.dataset.chatId = chat.id;

        const name = document.createElement('div');
        name.className = 'chat-entry-name';
        name.textContent = repoLabel(chat.repoUrl);

        const sub = document.createElement('div');
        sub.className = 'chat-entry-sub';
        const lastUserMsg = [...chat.messages].reverse().find(m => m.role === 'user');
        const pipeline = pipelines.get(chat.id);
        if (pipeline && pipeline.status === 'running') {
            sub.textContent = 'Indexing…';
        } else {
            sub.textContent = lastUserMsg ? lastUserMsg.content : 'No questions yet';
        }

        const closeBtn = document.createElement('button');
        closeBtn.className = 'chat-entry-close';
        closeBtn.textContent = '\u00d7';
        closeBtn.title = 'Close chat';
        closeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            closeChat(chat.id);
        });

        entry.appendChild(name);
        entry.appendChild(sub);
        entry.appendChild(closeBtn);
        entry.addEventListener('click', () => switchToChat(chat.id));
        chatListEl.appendChild(entry);
    });
}

// ---------------------------------------------------------------------
// Chat lifecycle
// ---------------------------------------------------------------------

function openNewChatView() {
    activeChatId = null;
    renderChatList();
    hidePipelineSide();
    repoUrlInput.value = '';
    ingestStatus.textContent = '';
    ingestStatus.className = '';
    renderKnownRepos();
    showView('ingest');
    repoUrlInput.focus();
}

function renderKnownRepos() {
    if (ingestedRepos.size === 0) {
        knownReposEl.hidden = true;
        return;
    }
    knownReposEl.hidden = false;
    knownReposListEl.innerHTML = '';

    ingestedRepos.forEach((meta, url) => {
        const item = document.createElement('div');
        item.className = 'known-repo-item';

        const name = document.createElement('span');
        name.className = 'known-repo-name';
        name.textContent = repoLabel(url);

        const metaEl = document.createElement('span');
        metaEl.className = 'known-repo-meta';
        metaEl.textContent = `${meta.numChunks} chunks / ${meta.numFiles} files`;

        item.appendChild(name);
        item.appendChild(metaEl);
        item.addEventListener('click', () => createChatForRepo(url));
        knownReposListEl.appendChild(item);
    });
}

async function createChatForRepo(repoUrl) {
    chatCounter += 1;
    const chat = {
        id: `chat-${chatCounter}-${Date.now()}`,
        repoUrl,
        messages: [],
    };
    chats.push(chat);

    // Repo was already indexed in this session — show a quick "already
    // ready" recap in the pipeline panel instead of the full sequence.
    const cached = ingestedRepos.get(repoUrl);
    if (cached) {
        initPipeline(chat.id, repoUrl);
        const pipeline = pipelines.get(chat.id);
        pipeline.status = 'done';
        pipeline.stepIndex = PIPELINE_STEP_DEFS.length - 1;
        pipeline.stats = { numChunks: cached.numChunks, numFiles: cached.numFiles };
    }

    await saveConversation(chat);
    switchToChat(chat.id);

    if (cached) {
        animatePipelineStatsIn(chat.id);
        await sleep(1400);
        if (pipelines.get(chat.id) === pipeline_ref(chat.id)) {
            collapsePipelineSide(chat.id);
        }
    }
}

function pipeline_ref(chatId) {
    return pipelines.get(chatId);
}

function switchToChat(chatId) {
    activeChatId = chatId;
    const chat = chats.find(c => c.id === chatId);
    if (!chat) return;

    renderChatList();
    chatViewRepoEl.textContent = repoLabel(chat.repoUrl);
    renderMessages(chat);
    showView('chat');

    const pipeline = pipelines.get(chatId);
    if (pipeline && pipeline.status === 'running') {
        setQueryEnabled(false);
        queryInput.placeholder = 'Indexing repository…';
    } else {
        setQueryEnabled(true);
        queryInput.placeholder = 'How does the authentication work?';
    }

    if (pipeline && !pipeline.collapsed) {
        showPipelineSide(chatId);
    } else {
        hidePipelineSide();
    }
    updatePipelineReopenButton(chatId);

    queryInput.focus();
}

async function closeChat(chatId) {
    const idx = chats.findIndex(c => c.id === chatId);
    if (idx === -1) return;
    chats.splice(idx, 1);

    const pipeline = pipelines.get(chatId);
    if (pipeline && pipeline.timers) pipeline.timers.forEach(t => clearTimeout(t));
    pipelines.delete(chatId);

    await deleteConversation(chatId);

    if (activeChatId === chatId) {
        if (chats.length > 0) {
            switchToChat(chats[Math.max(0, idx - 1)].id);
        } else {
            activeChatId = null;
            renderChatList();
            hidePipelineSide();
            showView('empty');
        }
    } else {
        renderChatList();
    }
}

function getActiveChat() {
    return chats.find(c => c.id === activeChatId) || null;
}

// ---------------------------------------------------------------------
// Ingest flow (new chat) — kicks off the live indexing pipeline
// ---------------------------------------------------------------------

function setIngestBusy(busy) {
    ingestBtn.disabled = busy;
    repoUrlInput.disabled = busy;
}

async function handleStartChat() {
    const repoUrl = repoUrlInput.value.trim();

    if (!repoUrl) {
        ingestStatus.textContent = 'Please enter a repository URL.';
        ingestStatus.className = 'status-error';
        return;
    }

    if (ingestedRepos.has(repoUrl)) {
        await createChatForRepo(repoUrl);
        return;
    }

    ingestStatus.textContent = '';
    ingestStatus.className = '';
    setIngestBusy(true);

    // Create the chat immediately and switch to it — the pipeline runs
    // live in the panel beside the (still empty) conversation.
    chatCounter += 1;
    const chat = {
        id: `chat-${chatCounter}-${Date.now()}`,
        repoUrl,
        messages: [],
    };
    chats.push(chat);
    renderChatList();

    initPipeline(chat.id, repoUrl);
    switchToChat(chat.id);
    setIngestBusy(false);

    runIngestPipeline(chat, repoUrl);
}

async function runIngestPipeline(chat, repoUrl) {
    const chatId = chat.id;
    showPipelineSide(chatId);
    runFakeProgression(chatId);

    try {
        const response = await fetch(`${API_BASE_URL}/ingest`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_url: repoUrl })
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || 'Ingestion failed.');
        }

        clearFakeProgression(chatId);
        ingestedRepos.set(repoUrl, { numChunks: data.num_chunks, numFiles: data.num_files });

        await completePipelineSuccess(chatId, {
            numFiles: data.num_files,
            numChunks: data.num_chunks,
        });

        chat.messages.push({
            role: 'system',
            content: `Indexed ${data.num_chunks} chunks across ${data.num_files} files. Ask away.`,
        });

        if (getActiveChat()?.id === chatId) {
            renderMessages(chat);
            setQueryEnabled(true);
            queryInput.placeholder = 'How does the authentication work?';
            queryInput.focus();
        }
        renderChatList();
        await saveConversation(chat);
    } catch (error) {
        clearFakeProgression(chatId);
        let msg = error.message;
        if (msg === 'Failed to fetch') {
            msg = 'Could not connect to backend API server. Make sure "python api.py" is running at http://localhost:8000';
        }
        completePipelineError(chatId, msg);

        chat.messages.push({
            role: 'system',
            content: `Indexing failed: ${msg}`,
            isError: true,
        });

        if (getActiveChat()?.id === chatId) {
            renderMessages(chat);
        }
        renderChatList();
    }
}

// ---------------------------------------------------------------------
// Indexing Pipeline — clone -> chunk -> embed -> index -> ready
// Shown live in the panel beside the chat. Since the backend answers
// with a single response (no progress stream), the first four stages
// are paced client-side and the pipeline holds on "Indexing" —
// pulsing — until the real request actually resolves, so it never
// claims to be further along than it is.
// ---------------------------------------------------------------------

const ICON_CLONE = '<svg width="15" height="15" viewBox="0 0 20 20" fill="none"><path d="M10 3v9m0 0l-3.5-3.5M10 12l3.5-3.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 13v2a2 2 0 002 2h8a2 2 0 002-2v-2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_CHUNK = '<svg width="15" height="15" viewBox="0 0 20 20" fill="none"><path d="M10 3l7 4-7 4-7-4 7-4z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M3 11l7 4 7-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_EMBED = '<svg width="15" height="15" viewBox="0 0 20 20" fill="none"><circle cx="5" cy="5" r="1.5" fill="currentColor"/><circle cx="10" cy="5" r="1.5" fill="currentColor"/><circle cx="15" cy="5" r="1.5" fill="currentColor"/><circle cx="5" cy="10" r="1.5" fill="currentColor"/><circle cx="10" cy="10" r="1.5" fill="currentColor"/><circle cx="15" cy="10" r="1.5" fill="currentColor"/><circle cx="5" cy="15" r="1.5" fill="currentColor"/><circle cx="10" cy="15" r="1.5" fill="currentColor"/><circle cx="15" cy="15" r="1.5" fill="currentColor"/></svg>';
const ICON_INDEX = '<svg width="15" height="15" viewBox="0 0 20 20" fill="none"><ellipse cx="10" cy="5" rx="6" ry="2.3" stroke="currentColor" stroke-width="1.6"/><path d="M4 5v10c0 1.27 2.69 2.3 6 2.3s6-1.03 6-2.3V5" stroke="currentColor" stroke-width="1.6"/><path d="M4 10c0 1.27 2.69 2.3 6 2.3s6-1.03 6-2.3" stroke="currentColor" stroke-width="1.6"/></svg>';
const ICON_READY = '<svg width="15" height="15" viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="7.3" stroke="currentColor" stroke-width="1.6"/><path d="M6.8 10.2l2.1 2.1 4.3-4.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_CHECK_SMALL = '<svg width="15" height="15" viewBox="0 0 20 20" fill="none"><path d="M4.5 10.2l3.6 3.6L15.5 6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_ERROR = '<svg width="15" height="15" viewBox="0 0 20 20" fill="none"><path d="M10 3.5l7.5 13h-15l7.5-13z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M10 8.5v3.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="10" cy="14.2" r="0.9" fill="currentColor"/></svg>';

const PIPELINE_STEP_DEFS = [
    { title: 'Cloning repository', sub: 'Fetching source from GitHub', icon: ICON_CLONE },
    { title: 'Parsing & chunking', sub: 'Splitting code along AST boundaries', icon: ICON_CHUNK },
    { title: 'Generating embeddings', sub: 'Encoding chunks locally (MiniLM)', icon: ICON_EMBED },
    { title: 'Indexing', sub: 'Storing vectors in the Chroma store', icon: ICON_INDEX },
    { title: 'Ready', sub: 'Answers will cite real code', icon: ICON_READY },
];

const FAKE_STEP_DELAYS = [900, 2300, 3900]; // ms to reach step 1, 2, 3

function initPipeline(chatId, repoUrl) {
    pipelines.set(chatId, {
        repoUrl,
        stepIndex: 0,
        status: 'running', // 'running' | 'done' | 'error'
        stats: null,
        errorMsg: null,
        collapsed: false,
        timers: [],
    });
}

function runFakeProgression(chatId) {
    const pipeline = pipelines.get(chatId);
    if (!pipeline) return;

    setPipelineStep(chatId, 0, 'active');

    FAKE_STEP_DELAYS.forEach((delay, i) => {
        const nextIndex = i + 1;
        const t = setTimeout(() => {
            const p = pipelines.get(chatId);
            if (!p || p.status !== 'running') return;
            setPipelineStep(chatId, nextIndex, 'active');
        }, delay);
        pipeline.timers.push(t);
    });
}

function clearFakeProgression(chatId) {
    const pipeline = pipelines.get(chatId);
    if (!pipeline) return;
    pipeline.timers.forEach(t => clearTimeout(t));
    pipeline.timers = [];
}

function setPipelineStep(chatId, index, status) {
    const pipeline = pipelines.get(chatId);
    if (!pipeline) return;
    pipeline.stepIndex = index;
    if (status === 'done') pipeline.status = 'done';

    applyPipelineVisualState(chatId);

    const stepsEl = document.getElementById(`pipeline-steps-${chatId}`);
    if (stepsEl) {
        const iconEl = stepsEl.querySelector(`[data-step-index="${index}"] .pipeline-step-icon`);
        if (iconEl) {
            anime.animate(iconEl, { scale: [0.7, 1], duration: 380, ease: 'outBack(1.7)' });
        }
    }

    renderChatList();
}

async function completePipelineSuccess(chatId, stats) {
    const pipeline = pipelines.get(chatId);
    if (!pipeline) return;
    pipeline.stats = { numChunks: stats.numChunks, numFiles: stats.numFiles };

    const lastStepIndex = PIPELINE_STEP_DEFS.length - 1;
    for (let i = pipeline.stepIndex; i < lastStepIndex; i++) {
        setPipelineStep(chatId, i + 1, i + 1 === lastStepIndex ? 'done' : 'active');
        // eslint-disable-next-line no-await-in-loop
        await sleep(230);
    }
    pipeline.status = 'done';
    applyPipelineVisualState(chatId);
    animatePipelineStatsIn(chatId);
    updatePipelineReopenButton(chatId);
    renderChatList();

    await sleep(1700);
    if (pipelines.get(chatId) === pipeline && pipeline.status === 'done') {
        collapsePipelineSide(chatId);
    }
}

function completePipelineError(chatId, message) {
    const pipeline = pipelines.get(chatId);
    if (!pipeline) return;
    pipeline.status = 'error';
    pipeline.errorMsg = message;
    applyPipelineVisualState(chatId);
    updatePipelineReopenButton(chatId);
    renderChatList();
}

function buildPipelinePanel(chatId) {
    const pipeline = pipelines.get(chatId);
    if (!pipeline) return;

    pipelinePanelEl.innerHTML = '';

    const heading = document.createElement('div');
    heading.className = 'pipeline-heading';
    heading.innerHTML = `<div class="pipeline-title">Indexing pipeline</div><div class="pipeline-repo">${escapeHtml(pipeline.repoUrl)}</div>`;
    pipelinePanelEl.appendChild(heading);

    const stepsEl = document.createElement('div');
    stepsEl.className = 'pipeline-steps';
    stepsEl.id = `pipeline-steps-${chatId}`;

    PIPELINE_STEP_DEFS.forEach((def, i) => {
        const stepEl = document.createElement('div');
        stepEl.className = 'pipeline-step';
        stepEl.dataset.stepIndex = String(i);

        const iconWrap = document.createElement('div');
        iconWrap.className = 'pipeline-step-icon';
        iconWrap.innerHTML = def.icon;

        const body = document.createElement('div');
        body.className = 'pipeline-step-body';

        const title = document.createElement('div');
        title.className = 'pipeline-step-title';
        title.textContent = def.title;

        const sub = document.createElement('div');
        sub.className = 'pipeline-step-sub';
        sub.textContent = def.sub;

        body.appendChild(title);
        body.appendChild(sub);
        stepEl.appendChild(iconWrap);
        stepEl.appendChild(body);
        stepsEl.appendChild(stepEl);
    });

    pipelinePanelEl.appendChild(stepsEl);

    const statsEl = document.createElement('div');
    statsEl.className = 'pipeline-stats';
    statsEl.id = `pipeline-stats-${chatId}`;
    statsEl.hidden = true;
    pipelinePanelEl.appendChild(statsEl);

    const errorEl = document.createElement('div');
    errorEl.className = 'pipeline-error-box';
    errorEl.id = `pipeline-error-${chatId}`;
    errorEl.hidden = true;
    pipelinePanelEl.appendChild(errorEl);

    const collapseBtn = document.createElement('button');
    collapseBtn.type = 'button';
    collapseBtn.className = 'pipeline-collapse-btn';
    collapseBtn.textContent = 'Hide panel';
    collapseBtn.addEventListener('click', () => collapsePipelineSide(chatId));
    pipelinePanelEl.appendChild(collapseBtn);

    applyPipelineVisualState(chatId);

    anime.animate(`#pipeline-steps-${chatId} .pipeline-step`, {
        opacity: [0, 1],
        translateX: [16, 0],
        delay: anime.stagger(90),
        duration: 420,
        ease: 'outExpo',
    });
}

function applyPipelineVisualState(chatId) {
    const pipeline = pipelines.get(chatId);
    if (!pipeline) return;

    const stepsEl = document.getElementById(`pipeline-steps-${chatId}`);
    if (stepsEl) {
        const stepEls = stepsEl.querySelectorAll('.pipeline-step');
        stepEls.forEach((el, i) => {
            el.classList.remove('active', 'done', 'error');
            const iconEl = el.querySelector('.pipeline-step-icon');

            if (pipeline.status === 'error' && i === pipeline.stepIndex) {
                el.classList.add('error');
                if (iconEl) iconEl.innerHTML = ICON_ERROR;
            } else if (i < pipeline.stepIndex || (pipeline.status === 'done' && i <= pipeline.stepIndex)) {
                el.classList.add('done');
                if (iconEl) iconEl.innerHTML = ICON_CHECK_SMALL;
            } else if (i === pipeline.stepIndex && pipeline.status === 'running') {
                el.classList.add('active');
            }
        });
    }

    const statsEl = document.getElementById(`pipeline-stats-${chatId}`);
    if (statsEl) {
        if (pipeline.status === 'done' && pipeline.stats) {
            statsEl.hidden = false;
            statsEl.innerHTML =
                `<div class="pipeline-stat"><div class="pipeline-stat-value">${pipeline.stats.numChunks}</div><div class="pipeline-stat-label">Chunks</div></div>` +
                `<div class="pipeline-stat"><div class="pipeline-stat-value">${pipeline.stats.numFiles}</div><div class="pipeline-stat-label">Files</div></div>`;
        } else {
            statsEl.hidden = true;
        }
    }

    const errorEl = document.getElementById(`pipeline-error-${chatId}`);
    if (errorEl) {
        if (pipeline.status === 'error') {
            errorEl.hidden = false;
            errorEl.textContent = pipeline.errorMsg || 'Something went wrong during indexing.';
        } else {
            errorEl.hidden = true;
        }
    }
}

function animatePipelineStatsIn(chatId) {
    const statsEl = document.getElementById(`pipeline-stats-${chatId}`);
    if (!statsEl) return;
    anime.animate(statsEl.querySelectorAll('.pipeline-stat'), {
        opacity: [0, 1],
        translateY: [10, 0],
        delay: anime.stagger(80),
        duration: 420,
        ease: 'outExpo',
    });
}

function showPipelineSide(chatId) {
    pipelineSideEl.hidden = false;
    buildPipelinePanel(chatId);
    updatePipelineReopenButton(chatId);
}

function hidePipelineSide() {
    pipelineSideEl.hidden = true;
}

function collapsePipelineSide(chatId) {
    const pipeline = pipelines.get(chatId);
    if (pipeline) pipeline.collapsed = true;
    if (getActiveChat()?.id === chatId) {
        hidePipelineSide();
        updatePipelineReopenButton(chatId);
    }
}

function expandPipelineSide(chatId) {
    const pipeline = pipelines.get(chatId);
    if (pipeline) pipeline.collapsed = false;
    if (getActiveChat()?.id === chatId) {
        showPipelineSide(chatId);
    }
}

function updatePipelineReopenButton(chatId) {
    const pipeline = pipelines.get(chatId);
    if (!pipeline || !pipeline.collapsed) {
        pipelineReopenBtn.classList.remove('visible');
        return;
    }
    pipelineReopenBtn.classList.add('visible');
    const dot = pipelineReopenBtn.querySelector('.status-dot');
    if (pipeline.status === 'error') {
        pipelineReopenLabel.textContent = 'Indexing failed';
        if (dot) dot.style.background = 'var(--danger)';
    } else {
        const count = pipeline.stats ? `${pipeline.stats.numChunks} chunks` : 'Indexed';
        pipelineReopenLabel.textContent = count;
        if (dot) dot.style.background = 'var(--success)';
    }
}

// ---------------------------------------------------------------------
// Chat query flow
// ---------------------------------------------------------------------

function setQueryEnabled(enabled) {
    queryInput.disabled = !enabled;
    queryBtn.disabled = !enabled;
}

function renderMessages(chat) {
    messagesEl.innerHTML = '';

    if (chat.messages.length === 0) {
        const pipeline = pipelines.get(chat.id);
        const placeholder = document.createElement('p');
        placeholder.className = 'placeholder';
        placeholder.textContent = (pipeline && pipeline.status === 'running')
            ? 'Indexing this repository — track progress in the panel on the right.'
            : 'Ask a question about this repo to get started.';
        messagesEl.appendChild(placeholder);
        return;
    }

    chat.messages.forEach(msg => messagesEl.appendChild(renderMessage(msg)));
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function renderMessage(msg) {
    if (msg.role === 'system') {
        const wrap = document.createElement('div');
        wrap.className = 'message system';
        const pill = document.createElement('div');
        pill.className = 'system-pill' + (msg.isError ? '' : ' success');
        pill.innerHTML = `<span class="status-dot"></span><span>${escapeHtml(msg.content)}</span>`;
        wrap.appendChild(pill);
        return wrap;
    }

    const wrap = document.createElement('div');
    wrap.className = 'message ' + msg.role + (msg.pending ? ' pending' : '') + (msg.isError ? ' error' : '');

    const roleEl = document.createElement('div');
    roleEl.className = 'message-role';
    roleEl.textContent = msg.role === 'user' ? 'You' : 'Assistant';

    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';

    if (msg.pending) {
        bubble.innerHTML = '<span>Thinking</span><span class="thinking-dots"><span></span><span></span><span></span></span>';
    } else if (msg.role === 'user' || msg.isError) {
        bubble.textContent = msg.content;
    } else {
        bubble.innerHTML = `<div class="formatted-answer">${formatMarkdown(msg.content)}</div>`;

        if (msg.sources && msg.sources.length) {
            const details = document.createElement('details');
            details.className = 'sources-panel';

            const summary = document.createElement('summary');
            summary.textContent = `Sources (${msg.sources.length})`;
            details.appendChild(summary);

            const list = document.createElement('ul');
            list.className = 'source-list';
            msg.sources.forEach((s, i) => {
                const li = document.createElement('li');
                li.className = 'source-item';
                li.innerHTML = `<span class="source-index">[${i + 1}]</span><span class="source-label">${escapeHtml(s.label)}</span>`;
                list.appendChild(li);
            });
            details.appendChild(list);
            bubble.appendChild(details);
        }
    }

    wrap.appendChild(roleEl);
    wrap.appendChild(bubble);
    return wrap;
}

async function handleAsk() {
    const chat = getActiveChat();
    if (!chat) return;

    const question = queryInput.value.trim();
    if (!question) return;

    queryInput.value = '';
    chat.messages.push({ role: 'user', content: question });

    const pendingMsg = { role: 'assistant', content: '', pending: true };
    chat.messages.push(pendingMsg);

    renderMessages(chat);
    renderChatList();
    setQueryEnabled(false);

    try {
        const response = await fetch(`${API_BASE_URL}/query`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: question, repo_url: chat.repoUrl })
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || 'Query failed.');
        }

        pendingMsg.content = data.answer;
        pendingMsg.sources = data.sources || [];
        pendingMsg.pending = false;
    } catch (error) {
        let msg = error.message;
        if (msg === 'Failed to fetch') {
            msg = 'Could not connect to backend API server. Make sure "python api.py" is running at http://localhost:8000';
        }
        pendingMsg.content = `Error: ${msg}`;
        pendingMsg.pending = false;
        pendingMsg.isError = true;
    } finally {
        if (getActiveChat() === chat) {
            renderMessages(chat);
            setQueryEnabled(true);
            queryInput.focus();
        }
        renderChatList();
        debouncedSave(chat);
    }
}

function debouncedSave(chat) {
    if (saveDebounceTimer) clearTimeout(saveDebounceTimer);
    saveDebounceTimer = setTimeout(() => {
        saveConversation(chat).catch(err =>
            console.error('Auto-save failed:', err)
        );
    }, 500);
}

// ---------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------

function openSettings() {
    settingsOverlay.hidden = false;
}

function closeSettings() {
    settingsOverlay.hidden = true;
}

async function handleSwitchAccount() {
    closeSettings();
    try {
        await signOut();
    } catch (err) {
        console.error('Sign out failed:', err);
    }
    resetAppState();
    authCard.classList.remove('visible');
    setAuthMode(false);
    showScreen('auth');
    setTimeout(() => authCard.classList.add('visible'), 50);
}

async function handleSignOut() {
    closeSettings();
    try {
        await signOut();
    } catch (err) {
        console.error('Sign out failed:', err);
    }
    resetAppState();
    showScreen('landing');
    playLandingAnimations();
}

async function handleDeleteAccount() {
    closeSettings();

    showConfirm(
        'Delete Account',
        'This will permanently delete all your conversations and sign you out. This cannot be undone.',
        async () => {
            try {
                await deleteAccount();
            } catch (err) {
                console.error('Delete account failed:', err);
            }
            resetAppState();
            showScreen('landing');
            playLandingAnimations();
        }
    );
}

function resetAppState() {
    chats = [];
    activeChatId = null;
    chatCounter = 0;
    currentUser = null;
    ingestedRepos.clear();
    pipelines.forEach(p => { if (p.timers) p.timers.forEach(t => clearTimeout(t)); });
    pipelines.clear();
    chatListEl.innerHTML = '';
    authUsername.value = '';
    authPassword.value = '';
    authError.hidden = true;
    hidePipelineSide();
}

// ---------------------------------------------------------------------
// Confirm Dialog
// ---------------------------------------------------------------------

let confirmCallback = null;

function showConfirm(title, message, onConfirm) {
    confirmTitle.textContent = title;
    confirmMessage.textContent = message;
    confirmCallback = onConfirm;
    confirmDialog.hidden = false;
}

function hideConfirm() {
    confirmDialog.hidden = true;
    confirmCallback = null;
}

// ---------------------------------------------------------------------
// Markdown formatting + code highlighting
// ---------------------------------------------------------------------

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function escapeRegex(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// File:line citation, e.g. `auth.py:45-62` or `db.py:12` — rendered as
// a distinct chip instead of generic inline code, so it's immediately
// clear a statement is pointing at a specific place in a specific file.
const CITE_PATTERN = /^([\w.\-/]+\.\w+):(\d+)(-(\d+))?$/;

// A markdown table's separator row, e.g. "|---|:---:|---:|" or
// "--- | --- | ---" without leading/trailing pipes. Used to confirm a
// "| a | b |"-looking line is actually the header of a real table and
// not just a sentence that happens to contain a pipe character.
const TABLE_SEPARATOR_ROW = /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$/;

function isTableRow(line) {
    return line.includes('|') && line.trim() !== '';
}

function splitTableRow(line) {
    let row = line.trim();
    if (row.startsWith('|')) row = row.slice(1);
    if (row.endsWith('|')) row = row.slice(0, -1);
    return row.split('|').map((cell) => cell.trim());
}

function renderTable(headerLine, alignLine, bodyLines) {
    const headers = splitTableRow(headerLine);
    const aligns = splitTableRow(alignLine).map((cell) => {
        const left = cell.startsWith(':');
        const right = cell.endsWith(':');
        if (left && right) return 'center';
        if (right) return 'right';
        if (left) return 'left';
        return '';
    });

    const theadCells = headers
        .map((h, i) => `<th${aligns[i] ? ` style="text-align:${aligns[i]}"` : ''}>${formatInlineMarkdown(h)}</th>`)
        .join('');

    const bodyRows = bodyLines
        .map((line) => {
            const cells = splitTableRow(line);
            const tds = headers
                .map((_, i) => `<td${aligns[i] ? ` style="text-align:${aligns[i]}"` : ''}>${formatInlineMarkdown(cells[i] || '')}</td>`)
                .join('');
            return `<tr>${tds}</tr>`;
        })
        .join('');

    return `<div class="markdown-table-wrap"><table class="markdown-table"><thead><tr>${theadCells}</tr></thead><tbody>${bodyRows}</tbody></table></div>`;
}

function formatMarkdown(text) {
    if (!text) return '';

    // Fenced code blocks first — rendered as a single "line" with no
    // embedded newlines, so the list/heading/table pass below can't split it.
    let formatted = text.replace(/```(\w*)\n([\s\S]*?)```/g, (match, lang, code) => {
        return renderCodeBlock(lang, code.trim());
    });

    const lines = formatted.split('\n');
    const processedLines = [];
    let inList = false;

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const trimmed = line.trim();

        // Table: a row containing "|", immediately followed by a
        // separator row ("|---|---|" etc). Consume every contiguous
        // row after that as the table body, then skip ahead past it.
        if (isTableRow(trimmed) && i + 1 < lines.length && TABLE_SEPARATOR_ROW.test(lines[i + 1].trim())) {
            if (inList) { processedLines.push('</ul>'); inList = false; }
            const headerLine = trimmed;
            const alignLine = lines[i + 1].trim();
            const bodyLines = [];
            let j = i + 2;
            while (j < lines.length && isTableRow(lines[j].trim())) {
                bodyLines.push(lines[j].trim());
                j++;
            }
            processedLines.push(renderTable(headerLine, alignLine, bodyLines));
            i = j - 1;
            continue;
        }

        if (trimmed.startsWith('<div class="code-block">')) {
            if (inList) { processedLines.push('</ul>'); inList = false; }
            processedLines.push(trimmed);
            continue;
        }

        if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
            if (!inList) {
                processedLines.push('<ul class="markdown-list">');
                inList = true;
            }
            processedLines.push(`<li>${formatInlineMarkdown(trimmed.substring(2))}</li>`);
            continue;
        } else if (inList && trimmed === '') {
            processedLines.push('</ul>');
            inList = false;
        }

        if (trimmed.startsWith('### ')) {
            if (inList) { processedLines.push('</ul>'); inList = false; }
            processedLines.push(`<h3>${formatInlineMarkdown(trimmed.substring(4))}</h3>`);
        } else if (trimmed.startsWith('## ')) {
            if (inList) { processedLines.push('</ul>'); inList = false; }
            processedLines.push(`<h2>${formatInlineMarkdown(trimmed.substring(3))}</h2>`);
        } else if (trimmed.startsWith('# ')) {
            if (inList) { processedLines.push('</ul>'); inList = false; }
            processedLines.push(`<h1>${formatInlineMarkdown(trimmed.substring(2))}</h1>`);
        } else if (trimmed !== '') {
            processedLines.push(`<p class="answer-paragraph">${formatInlineMarkdown(trimmed)}</p>`);
        }
    }

    if (inList) processedLines.push('</ul>');
    return processedLines.join('\n');
}

function formatInlineMarkdown(str) {
    return escapeHtml(str)
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/`([^`]+)`/g, (match, code) => {
            const m = code.match(CITE_PATTERN);
            if (m) {
                const file = m[1];
                const lines = m[4] ? `${m[2]}\u2013${m[4]}` : m[2];
                return `<span class="cite-chip"><span class="cite-file">${file}</span><span class="cite-lines">:${lines}</span></span>`;
            }
            return `<code class="inline-code">${code}</code>`;
        });
}

// ─── Lightweight syntax highlighting (no external deps) ────────────

const KEYWORD_SETS = {
    python: ['def', 'return', 'class', 'import', 'from', 'if', 'elif', 'else', 'for', 'while', 'in', 'is',
        'not', 'and', 'or', 'try', 'except', 'finally', 'with', 'as', 'pass', 'break', 'continue', 'lambda',
        'yield', 'None', 'True', 'False', 'self', 'raise', 'global', 'nonlocal', 'async', 'await', 'assert', 'del'],
    javascript: ['function', 'return', 'const', 'let', 'var', 'if', 'else', 'for', 'while', 'in', 'of', 'class',
        'extends', 'new', 'this', 'import', 'export', 'from', 'default', 'try', 'catch', 'finally', 'async',
        'await', 'yield', 'typeof', 'instanceof', 'null', 'undefined', 'true', 'false', 'switch', 'case',
        'break', 'continue', 'throw', 'super', 'static', 'get', 'set'],
    typescript: ['function', 'return', 'const', 'let', 'var', 'if', 'else', 'for', 'while', 'in', 'of', 'class',
        'extends', 'new', 'this', 'import', 'export', 'from', 'default', 'try', 'catch', 'finally', 'async',
        'await', 'yield', 'typeof', 'instanceof', 'null', 'undefined', 'true', 'false', 'switch', 'case',
        'break', 'continue', 'throw', 'super', 'static', 'get', 'set', 'interface', 'type', 'implements',
        'public', 'private', 'protected', 'readonly', 'enum', 'namespace', 'declare'],
    json: ['true', 'false', 'null'],
    bash: ['if', 'then', 'else', 'fi', 'for', 'do', 'done', 'while', 'case', 'esac', 'function', 'echo',
        'export', 'local', 'return', 'set', 'in'],
};

const LANG_ALIASES = {
    py: 'python', python: 'python',
    js: 'javascript', javascript: 'javascript', jsx: 'javascript',
    ts: 'typescript', typescript: 'typescript', tsx: 'typescript',
    json: 'json',
    sh: 'bash', shell: 'bash', bash: 'bash',
};

function normalizeLang(lang) {
    return LANG_ALIASES[(lang || '').toLowerCase()] || null;
}

const BACKTICK = String.fromCharCode(96);

function buildTokenRegex(canonicalLang) {
    const keywords = KEYWORD_SETS[canonicalLang] || [];

    let commentSrc = null;
    if (canonicalLang === 'python' || canonicalLang === 'bash') {
        commentSrc = '#.*';
    } else if (canonicalLang === 'javascript' || canonicalLang === 'typescript') {
        commentSrc = '//.*';
    }

    let stringSrc = '"(?:[^"\\\\]|\\\\.)*"' + "|'(?:[^'\\\\]|\\\\.)*'";
    if (canonicalLang === 'javascript' || canonicalLang === 'typescript') {
        stringSrc += '|' + BACKTICK + '(?:[^' + BACKTICK + '\\\\]|\\\\.)*' + BACKTICK;
    }

    const numberSrc = '\\b\\d+\\.?\\d*\\b';
    const decoratorSrc = canonicalLang === 'python' ? '@[\\w.]+' : null;
    const kwSrc = keywords.length ? '\\b(?:' + keywords.map(escapeRegex).join('|') + ')\\b' : null;

    const parts = [];
    if (commentSrc) parts.push('(?<comment>' + commentSrc + ')');
    parts.push('(?<string>' + stringSrc + ')');
    if (decoratorSrc) parts.push('(?<dec>' + decoratorSrc + ')');
    parts.push('(?<number>' + numberSrc + ')');
    if (kwSrc) parts.push('(?<kw>' + kwSrc + ')');

    return new RegExp(parts.join('|'), 'g');
}

function tokenizeLineWithRegex(line, regex) {
    let result = '';
    let lastIndex = 0;
    let match;
    regex.lastIndex = 0;

    while ((match = regex.exec(line)) !== null) {
        if (match.index > lastIndex) {
            result += escapeHtml(line.slice(lastIndex, match.index));
        }
        const text = match[0];
        const g = match.groups || {};
        const cls = g.comment !== undefined ? 'tok-com'
            : g.string !== undefined ? 'tok-str'
                : g.dec !== undefined ? 'tok-dec'
                    : g.number !== undefined ? 'tok-num'
                        : g.kw !== undefined ? 'tok-kw'
                            : null;
        result += cls ? `<span class="${cls}">${escapeHtml(text)}</span>` : escapeHtml(text);
        lastIndex = match.index + text.length;
        if (text.length === 0) regex.lastIndex += 1;
    }
    if (lastIndex < line.length) {
        result += escapeHtml(line.slice(lastIndex));
    }
    return result;
}

// Renders each source line as its own row with a right-aligned line
// number gutter, so indentation and per-statement structure stay
// visually intact and it's obvious which file/line a snippet came
// from when read alongside the citation right above it.
function highlightCode(code, lang) {
    const canonical = normalizeLang(lang);
    const regex = canonical ? buildTokenRegex(canonical) : null;
    const lines = code.split('\n');

    return lines.map((line, i) => {
        const content = regex ? tokenizeLineWithRegex(line, regex) : escapeHtml(line);
        return `<div class="code-line"><span class="code-line-no">${i + 1}</span><span class="code-line-content">${content || '&nbsp;'}</span></div>`;
    }).join('');
}

function renderCodeBlock(lang, code) {
    const highlighted = highlightCode(code, lang);
    const langLabel = (lang || 'text').toLowerCase();
    const rawAttr = encodeURIComponent(code);

    return [
        '<div class="code-block">',
        '<div class="code-block-header">',
        '<div class="code-block-header-left"><span class="code-block-lang">', escapeHtml(langLabel), '</span></div>',
        '<button class="code-block-copy" type="button" data-raw="', rawAttr, '">',
        '<svg width="13" height="13" viewBox="0 0 16 16" fill="none"><rect x="5" y="5" width="9" height="9" rx="1.5" stroke="currentColor" stroke-width="1.3"/><path d="M3 10.5V3a1 1 0 011-1h7.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
        '<span>Copy</span></button>',
        '</div>',
        '<div class="code-block-body">', highlighted, '</div>',
        '</div>',
    ].join('');
}

// Copy-to-clipboard via delegation, since code blocks are injected as
// raw HTML rather than built with individually-wired listeners.
document.addEventListener('click', (e) => {
    const btn = e.target.closest('.code-block-copy');
    if (!btn) return;
    const raw = decodeURIComponent(btn.dataset.raw || '');
    const label = btn.querySelector('span:last-child');
    const prevLabel = label ? label.textContent : '';

    navigator.clipboard.writeText(raw).then(() => {
        btn.classList.add('copied');
        if (label) label.textContent = 'Copied';
        setTimeout(() => {
            btn.classList.remove('copied');
            if (label) label.textContent = prevLabel;
        }, 1500);
    }).catch(() => { });
});

// ---------------------------------------------------------------------
// Event Wiring
// ---------------------------------------------------------------------

// Landing
landingGetStarted.addEventListener('click', () => {
    authCard.classList.remove('visible');
    setAuthMode(true);
    showScreen('auth');
});

landingSignIn.addEventListener('click', () => {
    authCard.classList.remove('visible');
    setAuthMode(false);
    showScreen('auth');
});

// Auth
authBackBtn.addEventListener('click', () => {
    showScreen('landing');
    playLandingAnimations();
});

authForm.addEventListener('submit', handleAuthSubmit);

authToggleBtn.addEventListener('click', () => {
    setAuthMode(!isSignUpMode);
});

// App — new chat
newChatBtn.addEventListener('click', openNewChatView);
emptyNewChatBtn.addEventListener('click', openNewChatView);

// Ingest
ingestBtn.addEventListener('click', handleStartChat);
repoUrlInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') handleStartChat();
});

// Pipeline reopen
pipelineReopenBtn.addEventListener('click', () => {
    if (activeChatId) expandPipelineSide(activeChatId);
});

// Query
queryBtn.addEventListener('click', handleAsk);
queryInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !queryInput.disabled) handleAsk();
});

// Settings
settingsBtn.addEventListener('click', openSettings);
settingsBackdrop.addEventListener('click', closeSettings);
settingsCloseBtn.addEventListener('click', closeSettings);
settingsSwitchBtn.addEventListener('click', handleSwitchAccount);
settingsSignoutBtn.addEventListener('click', handleSignOut);
settingsDeleteBtn.addEventListener('click', handleDeleteAccount);

// Confirm dialog
confirmCancel.addEventListener('click', hideConfirm);
confirmOk.addEventListener('click', () => {
    if (confirmCallback) confirmCallback();
    hideConfirm();
});

// Keyboard: Escape closes settings/confirm
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        if (!confirmDialog.hidden) hideConfirm();
        else if (!settingsOverlay.hidden) closeSettings();
    }
});

// ---------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------

async function init() {
    try {
        const session = await getSession();
        if (session) {
            currentUser = session.user;
            await enterApp();
        } else {
            showScreen('landing');
            playLandingAnimations();
        }
    } catch (err) {
        console.error('Init error:', err);
        showScreen('landing');
        playLandingAnimations();
    }
}

init();