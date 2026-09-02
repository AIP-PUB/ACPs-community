window.APP_CONFIG = {
  backendBase: "http://localhost:9031",
  oidc: {
    enabled: true,
    issuer: "http://localhost:9080/realms/acps-leader",
    clientId: "leader-web",
    apiAudience: "leader-api",
    scope: "openid profile email",
  },
};
