(function setRuntimeConfig() {
    const defaults = {
        backendBase: '', // 走同源反向代理（web-nginx -> leader）
        apiVersion: 'v1', // API版本
        pollInterval: 5000, // 轮询间隔（毫秒）
        maxPollRetries: 60, // 最大轮询次数
        oidc: {
            enabled: false,
            issuer: '',
            realm: 'acps-leader',
            clientId: 'leader-web',
            apiAudience: 'leader-api',
            scope: 'openid profile email',
            redirectUri: window.location.origin + window.location.pathname,
            postLogoutRedirectUri: window.location.origin + window.location.pathname,
        },
    };
    const runtime = window.APP_CONFIG || {};
    window.APP_CONFIG = Object.assign({}, defaults, runtime, {
        oidc: Object.assign({}, defaults.oidc, runtime.oidc || {}),
    });
})();
