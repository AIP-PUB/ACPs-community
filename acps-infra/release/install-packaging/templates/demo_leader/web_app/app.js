/**
 * Tour Assistant Frontend - 使用新的 Leader API
 * 
 * 新 API 模式：
 * - POST /api/v1/submit：异步提交用户输入，返回 sessionId 和 activeTaskId
 * - GET /api/v1/result/{session_id}：轮询任务状态和结果
 * 
 * 响应类型：
 * - pending: 任务等待执行
 * - running: 任务执行中
 * - awaiting_input: 需要用户补充信息（反问）
 * - completed: 任务完成
 * - failed: 任务失败
 */

// =============================================================================
// DOM 元素引用
// =============================================================================

const els = {
    messages: document.getElementById('messages'),
    analysisJson: document.getElementById('analysisJson'),
    partnerResults: document.getElementById('partnerResults'),
    finalResponse: document.getElementById('finalResponse'),
    userInput: document.getElementById('userInput'),
    form: document.getElementById('inputForm'),
    newSessionBtn: document.getElementById('newSessionBtn'),
    sessionDisplay: document.getElementById('sessionDisplay'),
    modeDisplay: document.getElementById('modeDisplay'),
    modeSelector: document.getElementById('modeSelector'),
    statusDisplay: document.getElementById('statusDisplay'),
    progressDisplay: document.getElementById('progressDisplay'),
    logoutBtn: document.getElementById('logoutBtn'),
    authDisplay: document.getElementById('authDisplay'),
};

// =============================================================================
// 配置和状态
// =============================================================================

const CONFIG = window.APP_CONFIG || {};
const DEFAULT_BACKEND_BASE = 'http://127.0.0.1:9031';
const API_VERSION = CONFIG.apiVersion || 'v1';
const POLL_INTERVAL = CONFIG.pollInterval || 1000;
const MAX_POLL_RETRIES = CONFIG.maxPollRetries || 60;

const runtimeBackendBase = typeof CONFIG.backendBase === 'string' ? CONFIG.backendBase.trim() : null;
const BACKEND_BASE = runtimeBackendBase === '' ? '' : runtimeBackendBase || DEFAULT_BACKEND_BASE;
const BACKEND_BASE_PREFIX = BACKEND_BASE.endsWith('/') ? BACKEND_BASE.slice(0, -1) : BACKEND_BASE;
const OIDC = Object.assign({
    enabled: false,
    issuer: '',
    clientId: 'leader-web',
    scope: 'openid profile email',
    redirectUri: window.location.origin + window.location.pathname,
    postLogoutRedirectUri: window.location.origin + window.location.pathname,
}, CONFIG.oidc || {});

// 应用状态
let state = {
    sessionId: null,
    activeTaskId: null,
    mode: 'direct_rpc',  // 执行模式: direct_rpc | group
    isSending: false,
    pollTimer: null,
    pollCount: 0,
    lastTaskStatus: null,
};

const authState = {
    discovery: null,
    accessToken: null,
    refreshToken: null,
    idToken: null,
    expiresAt: 0,
    profile: null,
    bootstrapPromise: null,
};

// =============================================================================
// API 工具函数
// =============================================================================

/**
 * 构建 API URL
 */
function apiUrl(path) {
    if (location.port === '9031' || BACKEND_BASE === '') return path;
    return BACKEND_BASE_PREFIX + path;
}

function oidcEnabled() {
    return Boolean(OIDC.enabled && typeof OIDC.issuer === 'string' && OIDC.issuer.trim());
}

function redirectUri() {
    return (OIDC.redirectUri || (window.location.origin + window.location.pathname)).trim();
}

function postLogoutRedirectUri() {
    return (OIDC.postLogoutRedirectUri || redirectUri()).trim();
}

function authStorageKey(name) {
    return `leader-oidc-${name}`;
}

function updateAuthUi() {
    const enabled = oidcEnabled();
    const display = authState.profile?.name || authState.profile?.preferred_username || authState.profile?.email || '';

    if (els.logoutBtn) {
        els.logoutBtn.classList.toggle('hidden', !enabled);
    }

    if (!els.authDisplay) {
        return;
    }

    if (!enabled || !display) {
        els.authDisplay.classList.add('hidden');
        els.authDisplay.textContent = '';
        return;
    }

    els.authDisplay.classList.remove('hidden');
    els.authDisplay.textContent = display;
}

function base64UrlEncode(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    bytes.forEach(byte => {
        binary += String.fromCharCode(byte);
    });
    return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

function randomString() {
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    return base64UrlEncode(bytes);
}

async function sha256Base64Url(value) {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
    return base64UrlEncode(digest);
}

async function loadOidcDiscovery() {
    if (authState.discovery) {
        return authState.discovery;
    }

    const issuer = OIDC.issuer.replace(/\/$/, '');
    const response = await fetch(`${issuer}/.well-known/openid-configuration`, {
        headers: { 'Accept': 'application/json' },
    });
    if (!response.ok) {
        throw new Error('无法加载 OIDC discovery 文档');
    }
    authState.discovery = await response.json();
    return authState.discovery;
}

function saveLoginTransaction({ state, verifier, nonce }) {
    sessionStorage.setItem(authStorageKey('state'), state);
    sessionStorage.setItem(authStorageKey('verifier'), verifier);
    sessionStorage.setItem(authStorageKey('nonce'), nonce);
}

function loadLoginTransaction() {
    return {
        state: sessionStorage.getItem(authStorageKey('state')),
        verifier: sessionStorage.getItem(authStorageKey('verifier')),
        nonce: sessionStorage.getItem(authStorageKey('nonce')),
    };
}

function clearLoginTransaction() {
    sessionStorage.removeItem(authStorageKey('state'));
    sessionStorage.removeItem(authStorageKey('verifier'));
    sessionStorage.removeItem(authStorageKey('nonce'));
}

function clearAuthState() {
    authState.accessToken = null;
    authState.refreshToken = null;
    authState.idToken = null;
    authState.expiresAt = 0;
    authState.profile = null;
    updateAuthUi();
}

function decodeJwtPayload(token) {
    const parts = token.split('.');
    if (parts.length < 2) {
        return {};
    }
    const payload = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const padded = payload + '='.repeat((4 - (payload.length % 4)) % 4);
    try {
        return JSON.parse(atob(padded));
    } catch (error) {
        console.warn('无法解析 token payload', error);
        return {};
    }
}

function storeTokenResponse(tokenResponse) {
    authState.accessToken = tokenResponse.access_token || null;
    authState.refreshToken = tokenResponse.refresh_token || null;
    authState.idToken = tokenResponse.id_token || null;

    const accessClaims = authState.accessToken ? decodeJwtPayload(authState.accessToken) : {};
    const now = Math.floor(Date.now() / 1000);
    authState.expiresAt = Number(accessClaims.exp || (now + Number(tokenResponse.expires_in || 60)));
    authState.profile = authState.idToken ? decodeJwtPayload(authState.idToken) : accessClaims;
    updateAuthUi();
}

async function exchangeAuthorizationCode(code, returnedState) {
    const discovery = await loadOidcDiscovery();
    const tx = loadLoginTransaction();
    if (!tx.state || !tx.verifier || tx.state !== returnedState) {
        clearAuthState();
        clearLoginTransaction();
        throw new Error('OIDC state 校验失败');
    }

    const body = new URLSearchParams({
        grant_type: 'authorization_code',
        code: code,
        client_id: OIDC.clientId,
        code_verifier: tx.verifier,
        redirect_uri: redirectUri(),
    });

    const response = await fetch(discovery.token_endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: body.toString(),
    });
    if (!response.ok) {
        clearAuthState();
        clearLoginTransaction();
        throw new Error('OIDC 授权码换 token 失败');
    }

    const tokenResponse = await response.json();
    const idTokenClaims = tokenResponse.id_token ? decodeJwtPayload(tokenResponse.id_token) : null;
    if (!tokenResponse.id_token || idTokenClaims?.nonce !== tx.nonce) {
        clearAuthState();
        clearLoginTransaction();
        throw new Error('OIDC nonce 校验失败');
    }

    clearLoginTransaction();
    storeTokenResponse(tokenResponse);

    const cleanUrl = new URL(window.location.href);
    cleanUrl.searchParams.delete('code');
    cleanUrl.searchParams.delete('state');
    cleanUrl.searchParams.delete('session_state');
    history.replaceState({}, document.title, cleanUrl.pathname + cleanUrl.search + cleanUrl.hash);
}

async function redirectToLogin() {
    const discovery = await loadOidcDiscovery();
    const stateValue = randomString();
    const verifier = randomString();
    const nonce = randomString();
    const challenge = await sha256Base64Url(verifier);
    saveLoginTransaction({ state: stateValue, verifier, nonce });

    const url = new URL(discovery.authorization_endpoint);
    url.searchParams.set('client_id', OIDC.clientId);
    url.searchParams.set('response_type', 'code');
    url.searchParams.set('redirect_uri', redirectUri());
    url.searchParams.set('scope', OIDC.scope || 'openid profile email');
    url.searchParams.set('state', stateValue);
    url.searchParams.set('nonce', nonce);
    url.searchParams.set('code_challenge', challenge);
    url.searchParams.set('code_challenge_method', 'S256');
    window.location.assign(url.toString());
}

async function refreshAccessToken() {
    if (!authState.refreshToken) {
        await redirectToLogin();
        return null;
    }

    const discovery = await loadOidcDiscovery();
    const body = new URLSearchParams({
        grant_type: 'refresh_token',
        client_id: OIDC.clientId,
        refresh_token: authState.refreshToken,
    });

    const response = await fetch(discovery.token_endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: body.toString(),
    });
    if (!response.ok) {
        clearAuthState();
        await redirectToLogin();
        return null;
    }

    const tokenResponse = await response.json();
    storeTokenResponse(tokenResponse);
    return authState.accessToken;
}

async function ensureAccessToken() {
    if (!oidcEnabled()) {
        return null;
    }

    if (!authState.accessToken) {
        await bootstrapAuth();
    }

    const now = Math.floor(Date.now() / 1000);
    if (authState.expiresAt && authState.expiresAt - now <= 30) {
        return refreshAccessToken();
    }
    return authState.accessToken;
}

async function apiFetch(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (!headers.has('Content-Type') && options.body !== undefined) {
        headers.set('Content-Type', 'application/json');
    }
    if (oidcEnabled()) {
        const accessToken = await ensureAccessToken();
        if (accessToken) {
            headers.set('Authorization', `Bearer ${accessToken}`);
        }
    }

    return fetch(apiUrl(path), Object.assign({}, options, { headers }));
}

async function bootstrapAuth() {
    if (!oidcEnabled()) {
        updateAuthUi();
        return;
    }
    if (authState.bootstrapPromise) {
        return authState.bootstrapPromise;
    }

    authState.bootstrapPromise = (async () => {
        const params = new URLSearchParams(window.location.search);
        if (params.get('error')) {
            throw new Error(params.get('error_description') || params.get('error') || 'OIDC 登录失败');
        }
        if (params.get('code')) {
            await exchangeAuthorizationCode(params.get('code'), params.get('state'));
            return;
        }
        if (authState.accessToken) {
            updateAuthUi();
            return;
        }
        await redirectToLogin();
    })();

    try {
        await authState.bootstrapPromise;
    } finally {
        authState.bootstrapPromise = null;
    }
}

async function logout() {
    const discovery = await loadOidcDiscovery().catch(() => null);
    const idTokenHint = authState.idToken;
    clearAuthState();
    clearLoginTransaction();

    if (discovery?.end_session_endpoint) {
        const url = new URL(discovery.end_session_endpoint);
        url.searchParams.set('post_logout_redirect_uri', postLogoutRedirectUri());
        if (idTokenHint) {
            url.searchParams.set('id_token_hint', idTokenHint);
        }
        window.location.assign(url.toString());
        return;
    }

    window.location.assign(postLogoutRedirectUri());
}

/**
 * 生成唯一的客户端请求 ID
 */
function generateClientRequestId() {
    return `req_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
}

/**
 * 提交用户请求到 /api/v1/submit
 * 
 * API 格式:
 * - query: 用户输入文本
 * - mode: 执行模式 (direct_rpc / group)
 * - sessionId: 会话 ID（可选，不传则创建新会话）
 * - clientRequestId: 客户端请求 ID（幂等性）
 * - activeTaskId: 乐观校验用的任务 ID（可选）
 */
async function submitQuery(query) {
    const payload = {
        query: query,
        mode: state.mode,  // 使用用户选择的模式
        clientRequestId: generateClientRequestId(),
    };

    if (state.sessionId) {
        payload.sessionId = state.sessionId;
    }

    // 如果有活跃任务 ID，添加用于乐观锁校验
    if (state.activeTaskId) {
        payload.activeTaskId = state.activeTaskId;
    }

    const response = await apiFetch(`/api/${API_VERSION}/submit`, {
        method: 'POST',
        body: JSON.stringify(payload),
    });

    const json = await response.json();

    // 新响应格式：CommonResponse { result, error }
    if (!response.ok || json.error) {
        const errorInfo = json.error || { code: response.status, message: json.detail?.message || 'Unknown error' };
        throw new Error(`[${errorInfo.code}] ${errorInfo.message}`);
    }

    return json;
}

/**
 * 查询任务结果 GET /api/v1/result/{session_id}
 * 
 * 响应格式 (LeaderResult):
 * - sessionId, mode, userId
 * - createdAt, updatedAt, touchedAt, expiresAt
 * - baseScenario, expertScenario
 * - activeTask: { id, status, createdAt, ... }
 * - partners: { [aic]: { state, data_items, ... } }
 * - userResult: { type, dataItems, updatedAt }
 * - dialogContext: { recentTurns, historySummary }
 */
async function getResult(sessionId, taskId = null) {
    let url = apiUrl(`/api/${API_VERSION}/result/${sessionId}`);
    if (taskId) {
        url += `?taskId=${encodeURIComponent(taskId)}`;
    }

    const response = await apiFetch(url.replace(BACKEND_BASE_PREFIX, ''), {
        method: 'GET',
    });

    const json = await response.json();

    // 新响应格式：CommonResponse { result, error }
    if (!response.ok || json.error) {
        const errorInfo = json.error || { code: response.status, message: 'Unknown error' };
        throw new Error(`[${errorInfo.code}] ${errorInfo.message}`);
    }

    return json;
}

// =============================================================================
// UI 渲染函数
// =============================================================================

/**
 * 添加聊天消息
 */
function addMessage(role, content) {
    const div = document.createElement('div');
    div.className = `message ${role}`;

    if (role === 'assistant' || role === 'system') {
        // 支持简单的 Markdown 渲染
        const safe = content
            .replace(/</g, '&lt;')
            .replace(/^# (.*)$/gm, '<h2>$1</h2>')
            .replace(/^## (.*)$/gm, '<h3>$1</h3>')
            .replace(/^\* (.*)$/gm, '<li>$1</li>')
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
            .replace(/\n\n/g, '<br/><br/>');
        const wrapped = safe.replace(/(<li>.*<\/li>)/gs, '<ul>$1</ul>');
        div.innerHTML = wrapped;
    } else {
        div.textContent = content;
    }

    els.messages.appendChild(div);
    els.messages.scrollTop = els.messages.scrollHeight;
}

/**
 * 设置会话 ID 显示
 */
function setSessionId(id) {
    state.sessionId = id;
    if (els.sessionDisplay) {
        els.sessionDisplay.textContent = id || '';
    }
    // 有 session 时隐藏模式选择器
    updateModeSelectorVisibility();
}

/**
 * 设置执行模式显示
 */
function setMode(mode) {
    state.mode = mode || 'direct_rpc';

    // 更新右上角徽章
    if (els.modeDisplay) {
        if (state.mode === 'group') {
            els.modeDisplay.textContent = '群组模式';
            els.modeDisplay.className = 'mode-badge mode-group';
        } else {
            els.modeDisplay.textContent = '直连模式';
            els.modeDisplay.className = 'mode-badge mode-direct';
        }
    }

    // 同步更新模式选择器
    const modeRadio = document.querySelector(`input[name="executionMode"][value="${state.mode}"]`);
    if (modeRadio) {
        modeRadio.checked = true;
    }
}

/**
 * 更新模式选择器的可见性
 * 有 session 时隐藏，没有时显示
 */
function updateModeSelectorVisibility() {
    if (els.modeSelector) {
        if (state.sessionId) {
            els.modeSelector.classList.add('hidden');
        } else {
            els.modeSelector.classList.remove('hidden');
        }
    }
}

/**
 * 更新状态显示
 */
function updateStatusDisplay(taskStatus, progress = null) {
    if (!els.statusDisplay) return;

    const statusMap = {
        'pending': '⏳ 等待处理...',
        'running': '🔄 处理中...',
        'awaiting_input': '❓ 需要补充信息',
        'completed': '✅ 已完成',
        'failed': '❌ 处理失败',
        'cancelled': '🚫 已取消',
    };

    els.statusDisplay.textContent = statusMap[taskStatus] || taskStatus || '';
    els.statusDisplay.className = `status-badge status-${taskStatus || 'unknown'}`;

    // 更新进度显示
    if (els.progressDisplay && progress) {
        const { total_partners, completed_partners, current_phase } = progress;
        if (total_partners > 0) {
            els.progressDisplay.textContent =
                `进度: ${completed_partners}/${total_partners} Partners | ${current_phase || ''}`;
        }
    }
}

/**
 * 重置 UI 到初始状态
 */
function resetSessionUI() {
    // 停止轮询
    stopPolling();

    // 重置状态
    state.sessionId = null;
    state.activeTaskId = null;
    state.isSending = false;
    state.pollCount = 0;
    state.lastTaskStatus = null;
    // 注意：不重置 state.mode，保留用户的选择

    // 重置 UI
    setSessionId(null);
    updateModeSelectorVisibility();  // 显示模式选择器
    els.messages.innerHTML = '';

    if (els.analysisJson) {
        els.analysisJson.textContent = '等待发送第一条消息...';
        els.analysisJson.classList.add('placeholder');
    }
    if (els.partnerResults) {
        els.partnerResults.innerHTML = '暂无结果';
        els.partnerResults.classList.add('placeholder');
    }
    if (els.finalResponse) {
        els.finalResponse.textContent = '暂无结果';
        els.finalResponse.classList.add('placeholder');
    }
    if (els.statusDisplay) {
        els.statusDisplay.textContent = '';
    }
    if (els.progressDisplay) {
        els.progressDisplay.textContent = '';
    }
}

/**
 * 清空三个显示区域（发送新请求时调用）
 */
function clearDisplayPanels() {
    if (els.analysisJson) {
        els.analysisJson.textContent = '正在分析请求...';
        els.analysisJson.classList.add('placeholder');
    }
    if (els.partnerResults) {
        els.partnerResults.innerHTML = '正在调度 Agent...';
        els.partnerResults.classList.add('placeholder');
    }
    if (els.finalResponse) {
        els.finalResponse.textContent = '等待处理结果...';
        els.finalResponse.classList.add('placeholder');
    }
}

/**
 * 格式化 JSON 显示
 */
function pretty(obj) {
    return JSON.stringify(obj, null, 2);
}

/**
 * 渲染分析结果 - 支持新旧格式
 */
function renderAnalysis(data) {
    if (!els.analysisJson) return;

    const analysis = {
        sessionId: data.sessionId,
        activeTaskId: data.activeTaskId,
        mode: data.mode,
        externalStatus: data.externalStatus,
        taskStatus: data.taskStatus,
        baseScenario: data.baseScenario,
        expertScenario: data.expertScenario,
        userResultType: data.userResultType,
        acceptedAt: data.acceptedAt,
    };

    // 过滤掉 undefined 值
    Object.keys(analysis).forEach(key => analysis[key] === undefined && delete analysis[key]);

    els.analysisJson.textContent = pretty(analysis);
    els.analysisJson.classList.remove('placeholder');
}

/**
 * 渲染 Partner 状态 - 使用 activeTask.partnerTasks 格式
 * 
 * partnerTasks 格式: { [aic]: { partnerAic, aipTaskId, state, dimensions, lastStateChangedAt, ... } }
 */
function renderPartnerResultsNew(partnerTasks) {
    if (!els.partnerResults) return;

    els.partnerResults.classList.remove('placeholder');
    els.partnerResults.innerHTML = '';

    if (!partnerTasks || Object.keys(partnerTasks).length === 0) {
        els.partnerResults.innerHTML = '<div class="placeholder">暂无 Agent 状态数据</div>';
        return;
    }

    // 直接显示每个 partner 的详细状态（不显示统计汇总）
    Object.entries(partnerTasks).forEach(([aic, data]) => {
        const card = document.createElement('div');
        card.className = 'partner-card';

        const partnerState = data.state || 'unknown';
        if (partnerState === 'awaiting-input') {
            card.classList.add('clarification');
        }

        const title = document.createElement('h3');
        // 优先显示 partner 名称，否则显示截断的 AIC
        const partnerName = data.partnerName || data.partner_name;
        let displayName;
        if (partnerName) {
            // 名称超过 16 个字符时截断
            displayName = partnerName.length > 16 ? partnerName.slice(0, 14) + '...' : partnerName;
        } else {
            // 无名称时显示 AIC 后缀
            displayName = aic.length > 12 ? '...' + aic.slice(-8) : aic;
        }
        title.innerHTML = `<span title="${partnerName || aic}">${displayName}</span><code class="partner-state state-${partnerState}">${partnerState}</code>`;

        const content = document.createElement('div');
        content.style.fontSize = '11px';
        content.style.maxHeight = '200px';
        content.style.overflow = 'auto';

        // 显示维度信息
        const dimensions = data.dimensions || [];
        if (dimensions.length > 0) {
            const dimInfo = document.createElement('div');
            dimInfo.style.color = '#666';
            dimInfo.innerHTML = `<strong>维度:</strong> ${dimensions.join(', ')}`;
            content.appendChild(dimInfo);
        }

        // 显示更新时间
        if (data.lastStateChangedAt || data.last_state_changed_at) {
            const timeInfo = document.createElement('div');
            timeInfo.style.color = '#888';
            timeInfo.style.marginTop = '4px';
            timeInfo.textContent = `更新: ${new Date(data.lastStateChangedAt || data.last_state_changed_at).toLocaleTimeString()}`;
            content.appendChild(timeInfo);
        }

        // 显示 Partner 结果数据（截断显示，鼠标悬停显示完整内容）
        const dataItems = data.dataItems || data.data_items || [];
        if (dataItems.length > 0) {
            const resultDiv = document.createElement('div');
            resultDiv.style.marginTop = '8px';
            resultDiv.style.borderTop = '1px solid #eee';
            resultDiv.style.paddingTop = '6px';

            const resultLabel = document.createElement('strong');
            resultLabel.textContent = '结果数据:';
            resultLabel.style.color = '#333';
            resultDiv.appendChild(resultLabel);

            // 提取文本内容
            let resultText = '';
            dataItems.forEach((item, idx) => {
                const text = item.text || item.content || (typeof item === 'string' ? item : JSON.stringify(item));
                if (text) {
                    resultText += (idx > 0 ? '\n' : '') + text;
                }
            });

            if (resultText) {
                const resultContent = document.createElement('div');
                resultContent.style.marginTop = '4px';
                resultContent.style.color = '#555';
                resultContent.style.fontSize = '10px';
                resultContent.style.lineHeight = '1.4';
                resultContent.style.cursor = 'pointer';

                // 截断显示（最多 150 个字符）
                const maxLen = 150;
                const truncated = resultText.length > maxLen ? resultText.slice(0, maxLen) + '...' : resultText;
                resultContent.textContent = truncated;

                // 鼠标悬停显示完整内容（使用 title 属性）
                resultContent.title = resultText;

                // 添加悬停样式提示
                resultContent.style.backgroundColor = '#f9f9f9';
                resultContent.style.padding = '4px 6px';
                resultContent.style.borderRadius = '4px';
                resultContent.style.whiteSpace = 'pre-wrap';
                resultContent.style.wordBreak = 'break-word';

                resultDiv.appendChild(resultContent);
            }

            content.appendChild(resultDiv);
        }

        card.appendChild(title);
        card.appendChild(content);
        els.partnerResults.appendChild(card);
    });
}

/**
 * 渲染 Partner 状态（显示各 Agent 的执行状态）
 */
function renderPartnerResults(resultData, metadata) {
    if (!els.partnerResults) return;

    els.partnerResults.classList.remove('placeholder');
    els.partnerResults.innerHTML = '';

    // 从 resultData 中提取 execution_result
    const executionResult = resultData?.execution_result || {};
    const partnerResults = executionResult.partner_results || {};

    // 显示整体执行阶段
    if (executionResult.phase) {
        const phaseInfo = document.createElement('div');
        phaseInfo.className = 'execution-phase-info';
        phaseInfo.innerHTML = `<strong>执行阶段:</strong> <code>${executionResult.phase}</code>`;
        els.partnerResults.appendChild(phaseInfo);
    }

    // 显示 partner 状态汇总
    const summary = document.createElement('div');
    summary.className = 'partner-summary';
    summary.innerHTML = `
        <div class="summary-item">
            <span class="label">等待输入:</span>
            <span class="value">${(executionResult.awaiting_input_partners || []).length}</span>
        </div>
        <div class="summary-item">
            <span class="label">等待确认:</span>
            <span class="value">${(executionResult.awaiting_completion_partners || []).length}</span>
        </div>
        <div class="summary-item">
            <span class="label">已完成:</span>
            <span class="value">${(executionResult.completed_partners || []).length}</span>
        </div>
        <div class="summary-item">
            <span class="label">失败:</span>
            <span class="value">${(executionResult.failed_partners || []).length}</span>
        </div>
    `;
    els.partnerResults.appendChild(summary);

    // 如果有 partner_results，显示每个 partner 的详细状态
    if (Object.keys(partnerResults).length > 0) {
        const detailsTitle = document.createElement('h4');
        detailsTitle.textContent = '各 Agent 详细状态:';
        detailsTitle.style.marginTop = '12px';
        els.partnerResults.appendChild(detailsTitle);

        Object.entries(partnerResults).forEach(([partnerId, data]) => {
            const card = document.createElement('div');
            card.className = 'partner-card';

            // 根据状态设置样式
            const partnerState = data.state || 'unknown';
            if (partnerState === 'awaiting-input') {
                card.classList.add('clarification');
            }

            const title = document.createElement('h3');
            // 截取 partnerId 的最后几位用于显示
            const shortId = partnerId.length > 16 ? '...' + partnerId.slice(-12) : partnerId;
            title.innerHTML = `<span title="${partnerId}">${shortId}</span><code class="partner-state state-${partnerState}">${partnerState}</code>`;

            const content = document.createElement('pre');
            content.style.fontSize = '11px';
            content.style.maxHeight = '150px';
            content.style.overflow = 'auto';
            content.textContent = pretty(data);

            card.appendChild(title);
            card.appendChild(content);
            els.partnerResults.appendChild(card);
        });
    } else if (!resultData) {
        els.partnerResults.innerHTML = '<div class="placeholder">暂无 Agent 状态数据</div>';
    }
}

/**
 * 渲染整合结果（包括反问和最终响应）
 */
function renderFinalResponse(text, resultData, taskStatus) {
    if (!els.finalResponse) return;

    els.finalResponse.classList.remove('placeholder');
    els.finalResponse.innerHTML = '';

    // 根据任务状态显示不同内容
    if (taskStatus === 'awaiting_input') {
        // 反问场景
        const clarificationText = resultData?.clarification_text || text;
        if (clarificationText) {
            const header = document.createElement('div');
            header.className = 'clarification-header';
            header.innerHTML = '<strong>⚠️ 需要补充信息</strong>';
            els.finalResponse.appendChild(header);

            const content = document.createElement('div');
            content.className = 'clarification-content';
            content.innerHTML = clarificationText.replace(/\n/g, '<br/>');
            els.finalResponse.appendChild(content);
        } else {
            els.finalResponse.textContent = '等待用户输入...';
        }
        return;
    }

    if (taskStatus === 'completed') {
        // 完成场景
        if (text) {
            // 基础 Markdown 转 HTML
            const safe = text
                .replace(/</g, '&lt;')
                .replace(/^# (.*)$/gm, '<h2>$1</h2>')
                .replace(/^## (.*)$/gm, '<h3>$1</h3>')
                .replace(/^\* (.*)$/gm, '<li>$1</li>')
                .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
                .replace(/\n\n/g, '<br/><br/>');

            const wrapped = safe.replace(/(<li>.*<\/li>)/gs, '<ul>$1</ul>');
            els.finalResponse.innerHTML = wrapped;
        } else {
            els.finalResponse.textContent = '任务已完成';
        }
        return;
    }

    if (taskStatus === 'failed') {
        // 失败场景
        const errorMsg = resultData?.error_message || '任务执行失败';
        els.finalResponse.innerHTML = `<div class="error-message">❌ ${errorMsg}</div>`;
        return;
    }

    // 其他状态（pending, running）
    if (!text) {
        els.finalResponse.textContent = '处理中...';
        els.finalResponse.classList.add('placeholder');
        return;
    }

    // pending/running 状态下显示进度信息
    if (taskStatus === 'pending' || taskStatus === 'running') {
        const header = document.createElement('div');
        header.className = 'progress-header';
        header.innerHTML = '<strong>⏳ 任务处理中...</strong>';
        els.finalResponse.appendChild(header);

        const content = document.createElement('div');
        content.className = 'progress-content';
        // 基础 Markdown 转 HTML
        const safe = text
            .replace(/</g, '&lt;')
            .replace(/^# (.*)$/gm, '<h2>$1</h2>')
            .replace(/^## (.*)$/gm, '<h3>$1</h3>')
            .replace(/^\* (.*)$/gm, '<li>$1</li>')
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
            .replace(/\n\n/g, '<br/><br/>');
        const wrapped = safe.replace(/(<li>.*<\/li>)/gs, '<ul>$1</ul>');
        content.innerHTML = wrapped;
        els.finalResponse.appendChild(content);
        return;
    }

    els.finalResponse.textContent = text;
}

// =============================================================================
// 轮询逻辑
// =============================================================================

/**
 * 停止轮询
 */
function stopPolling() {
    if (state.pollTimer) {
        clearTimeout(state.pollTimer);
        state.pollTimer = null;
    }
}

/**
 * 开始轮询任务结果
 */
function startPolling(sessionId, taskId) {
    stopPolling();
    state.pollCount = 0;

    async function poll() {
        if (!state.sessionId || state.pollCount >= MAX_POLL_RETRIES) {
            console.log('轮询停止: 达到最大次数或会话已结束');
            finishPolling('timeout');
            return;
        }

        state.pollCount++;

        try {
            const result = await getResult(sessionId, taskId);
            handlePollResult(result);
        } catch (error) {
            console.error('轮询出错:', error);
            // 继续轮询，除非是致命错误
            state.pollTimer = setTimeout(poll, POLL_INTERVAL);
        }
    }

    // 开始第一次轮询
    poll();
}

/**
 * 处理轮询结果 - 新 API LeaderResult 格式
 * 
 * LeaderResult 结构:
 * - sessionId, mode, userId
 * - activeTask: { id, status, createdAt, ... }
 * - userResult: { type, dataItems, updatedAt }
 * - partners: { [aic]: { state, dataItems, ... } }
 * - dialogContext: { recentTurns, historySummary }
 */
function handlePollResult(response) {
    // 从 CommonResponse 中提取 result
    const result = response.result;
    if (!result) {
        console.error('Invalid poll response: missing result');
        state.pollTimer = setTimeout(() => startPolling(state.sessionId, state.activeTaskId), POLL_INTERVAL);
        return;
    }

    // 更新模式显示（从服务器响应确认）
    if (result.mode) {
        setMode(result.mode);
    }

    // 从 activeTask 获取任务状态
    const activeTask = result.activeTask;
    const userResult = result.userResult;

    // 优先检查 userResult.type 来判断是否完成
    // 对于闲聊（CHIT_CHAT）场景，没有 activeTask，但 userResult.type='final' 表示已完成
    const userResultType = userResult?.type;
    let taskStatus;

    if (userResultType === 'final' && !activeTask) {
        // 闲聊场景：无任务，但结果已就绪
        taskStatus = 'completed';
    } else if (userResultType === 'error') {
        // 错误状态
        taskStatus = 'failed';
    } else if (userResultType === 'clarification') {
        // 需要用户澄清
        taskStatus = 'awaiting_input';
    } else {
        // 其他情况按 activeTask 状态判断
        // 注意：后端返回的状态是大写（如 RUNNING），需要转换为小写
        const rawStatus = activeTask?.externalStatus || activeTask?.status || 'pending';
        taskStatus = rawStatus.toLowerCase();
    }

    // 更新状态显示
    updateStatusDisplay(taskStatus);
    state.lastTaskStatus = taskStatus;

    // 更新分析面板
    renderAnalysis({
        sessionId: result.sessionId,
        activeTaskId: activeTask?.id,
        mode: result.mode,
        taskStatus: taskStatus,
        baseScenario: result.baseScenario,
        expertScenario: result.expertScenario,
        userResultType: userResult?.type,
    });

    // 根据状态处理
    switch (taskStatus) {
        case 'pending':
        case 'running':
            // 显示正在处理的进度消息（如果有新内容）
            if (userResult?.dataItems?.length > 0) {
                const lastItem = userResult.dataItems[userResult.dataItems.length - 1];
                const progressText = lastItem.content || lastItem.text;
                if (progressText) {
                    // 更新右下角的整合结果面板，显示进度信息
                    renderFinalResponse(progressText, result, taskStatus);
                    // 更新 Partner 状态面板
                    renderPartnerResultsNew(result.activeTask?.partnerTasks);
                }
            }
            // 继续轮询
            state.pollTimer = setTimeout(() => startPolling(state.sessionId, state.activeTaskId), POLL_INTERVAL);
            break;

        case 'awaiting_input':
            // 需要用户补充信息
            finishPolling('awaiting_input', result);
            handleClarificationResult(result);
            break;

        case 'completed':
            // 任务完成
            finishPolling('completed', result);
            handleCompletedResult(result);
            break;

        case 'failed':
            // 任务失败
            finishPolling('failed', result);
            handleFailedResult(result);
            break;

        case 'cancelled':
            finishPolling('cancelled', result);
            addMessage('system', '任务已取消');
            break;

        default:
            // 未知状态，检查是否有 userResult
            if (userResult?.dataItems?.length > 0) {
                finishPolling('completed', result);
                handleCompletedResult(result);
            } else {
                // 继续轮询
                state.pollTimer = setTimeout(() => startPolling(state.sessionId, state.activeTaskId), POLL_INTERVAL);
            }
    }
}

/**
 * 完成轮询
 */
function finishPolling(reason, result = null) {
    stopPolling();
    state.isSending = false;
    enableInput();

    // 根据后端返回的 activeTask 同步本地状态
    // 如果后端没有 activeTask（如闲聊场景），则清除本地的 activeTaskId
    if (result) {
        const activeTask = result.activeTask;
        state.activeTaskId = activeTask?.id || null;
    }

    console.log(`轮询完成: ${reason}, activeTaskId=${state.activeTaskId}`);
}

/**
 * 处理需要补充信息的情况（反问） - 新 API LeaderResult 格式
 */
function handleClarificationResult(result) {
    const userResult = result.userResult || {};
    const dataItems = userResult.dataItems || [];

    // 从 dataItems 中提取最后一个响应
    let clarificationText = '请补充更多信息';
    if (dataItems.length > 0) {
        const lastItem = dataItems[dataItems.length - 1];
        clarificationText = lastItem.content || lastItem.text || clarificationText;
    }

    addMessage('assistant', `❓ ${clarificationText}`);
    renderPartnerResultsNew(result.activeTask?.partnerTasks);
    renderFinalResponse(clarificationText, result, 'awaiting_input');
}

/**
 * 处理任务完成 - 新 API LeaderResult 格式
 */
function handleCompletedResult(result) {
    const userResult = result.userResult || {};
    const dataItems = userResult.dataItems || [];

    // 从 dataItems 中提取最后一个响应
    let responseText = '任务已完成';
    if (dataItems.length > 0) {
        const lastItem = dataItems[dataItems.length - 1];
        responseText = lastItem.content || lastItem.text || responseText;
    }

    state.isSending = false;
    enableInput();
    updateStatusDisplay('completed');
    addMessage('assistant', responseText);
    renderPartnerResultsNew(result.activeTask?.partnerTasks);
    renderFinalResponse(responseText, result, 'completed');
}

/**
 * 处理任务失败 - 新 API LeaderResult 格式
 */
function handleFailedResult(result) {
    const userResult = result.userResult || {};
    const activeTask = result.activeTask || {};

    const errorMessage = activeTask.error ||
        userResult.error ||
        '任务处理失败';

    state.isSending = false;
    enableInput();
    updateStatusDisplay('failed');
    addMessage('assistant', `❌ 错误: ${errorMessage}`);
    renderFinalResponse(errorMessage, result, 'failed');
}

// 保留旧函数作为兼容层
function handleClarification(result) { return handleClarificationResult(result); }
function handleCompleted(result) { return handleCompletedResult(result); }
function handleFailed(result) { return handleFailedResult(result); }

// =============================================================================
// 输入控制
// =============================================================================

function disableInput() {
    if (els.form) {
        const btn = els.form.querySelector('button[type="submit"]');
        if (btn) btn.disabled = true;
    }
    if (els.userInput) {
        els.userInput.disabled = true;
    }
}

function enableInput() {
    if (els.form) {
        const btn = els.form.querySelector('button[type="submit"]');
        if (btn) btn.disabled = false;
    }
    if (els.userInput) {
        els.userInput.disabled = false;
        els.userInput.focus();
    }
}

// =============================================================================
// 主交互流程
// =============================================================================

/**
 * 发送用户查询
 * 
 * API 响应格式:
 * SubmitResponse.result = SubmitResult {
 *   sessionId, mode, activeTaskId, acceptedAt, externalStatus
 * }
 * 
 * externalStatus 值: pending, running, awaiting_input, completed, failed
 */
async function sendQuery(query) {
    if (!query.trim()) return;
    if (state.isSending) return;

    state.isSending = true;
    disableInput();
    addMessage('user', query);
    updateStatusDisplay('pending');

    // 清空三个显示区域，表示新的过程开始
    clearDisplayPanels();

    try {
        // 1. 提交请求
        const submitResponse = await submitQuery(query);

        // 2. 从 CommonResponse 中提取 result
        const result = submitResponse.result;
        if (!result) {
            throw new Error('Invalid response: missing result');
        }

        const sessionId = result.sessionId;
        const activeTaskId = result.activeTaskId;
        const externalStatus = result.externalStatus;
        const mode = result.mode;

        // 更新会话状态
        if (!state.sessionId && sessionId) {
            setSessionId(sessionId);
        }
        state.activeTaskId = activeTaskId;

        // 更新模式显示（从服务器响应确认）
        if (mode) {
            setMode(mode);
        }

        // 更新分析面板
        renderAnalysis({
            sessionId: sessionId,
            activeTaskId: activeTaskId,
            mode: mode,
            externalStatus: externalStatus,
            acceptedAt: result.acceptedAt,
        });

        // 3. 根据 externalStatus 处理
        if (externalStatus === 'completed') {
            // 任务已完成，获取结果
            const resultResponse = await getResult(sessionId, activeTaskId);
            const leaderResult = resultResponse.result;
            handleCompletedResult(leaderResult);
        } else if (externalStatus === 'awaiting_input') {
            // 需要补充信息
            state.isSending = false;
            enableInput();
            updateStatusDisplay('awaiting_input');
            // 获取当前状态以显示提示
            const resultResponse = await getResult(sessionId, activeTaskId);
            const leaderResult = resultResponse.result;
            // 渲染 Partner 状态和整合结果
            renderPartnerResultsNew(leaderResult?.activeTask?.partnerTasks);
            if (leaderResult?.userResult?.dataItems?.length > 0) {
                const lastItem = leaderResult.userResult.dataItems[leaderResult.userResult.dataItems.length - 1];
                const message = lastItem?.text || lastItem?.content || '请提供更多信息';
                addMessage('assistant', `❓ ${message}`);
                renderFinalResponse(message, leaderResult, 'awaiting_input');
            } else {
                addMessage('assistant', '❓ 请提供更多信息');
            }
        } else if (externalStatus === 'pending' || externalStatus === 'running') {
            // 异步任务，需要轮询
            updateStatusDisplay(externalStatus);
            startPolling(sessionId, activeTaskId);
        } else if (externalStatus === 'failed') {
            // 任务失败
            state.isSending = false;
            enableInput();
            updateStatusDisplay('failed');
            addMessage('assistant', '❌ 任务执行失败');
        } else {
            // 未知状态，尝试轮询
            updateStatusDisplay('running');
            startPolling(sessionId, activeTaskId);
        }

    } catch (error) {
        console.error('发送失败:', error);
        state.isSending = false;
        enableInput();
        updateStatusDisplay('failed');
        addMessage('assistant', `❌ 发送失败: ${error.message}`);
    }
}

// =============================================================================
// 事件绑定
// =============================================================================

// 表单提交
if (els.form) {
    els.form.addEventListener('submit', (e) => {
        e.preventDefault();
        const query = els.userInput.value;
        els.userInput.value = '';
        sendQuery(query);
    });
}

// 新会话按钮
if (els.newSessionBtn) {
    els.newSessionBtn.addEventListener('click', () => {
        resetSessionUI();
        addMessage('system', '已创建新的会话，请输入您的需求。');
    });
}

if (els.logoutBtn) {
    els.logoutBtn.addEventListener('click', () => {
        logout().catch(error => {
            console.error('退出登录失败:', error);
        });
    });
}

// 模式选择器事件
const modeRadios = document.querySelectorAll('input[name="executionMode"]');
modeRadios.forEach(radio => {
    radio.addEventListener('change', (e) => {
        if (!state.sessionId) {  // 只有没有 session 时才能切换
            setMode(e.target.value);
        }
    });
});

// 键盘快捷键
if (els.userInput) {
    els.userInput.addEventListener('keydown', (e) => {
        // Ctrl/Cmd + Enter 发送
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            e.preventDefault();
            els.form.dispatchEvent(new Event('submit'));
        }
    });
}

// =============================================================================
// 初始化
// =============================================================================

async function bootstrapApp() {
    resetSessionUI();
    disableInput();

    try {
        await bootstrapAuth();
        const welcome = oidcEnabled()
            ? '欢迎回来，输入您的需求开始对话。\n\n💡 提示：按 Ctrl+Enter 快速发送'
            : '欢迎！输入您的需求开始对话。\n\n💡 提示：按 Ctrl+Enter 快速发送';
        addMessage('system', welcome);
        enableInput();
    } catch (error) {
        console.error('应用初始化失败:', error);
        addMessage('assistant', `❌ 初始化失败: ${error.message}`);
    }
}

bootstrapApp();
