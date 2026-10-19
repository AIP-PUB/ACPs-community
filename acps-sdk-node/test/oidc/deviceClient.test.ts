/**
 * 移植自 acps-sdk/tests/oidc/test_device_client.py。
 *
 * Python 版 `OidcDeviceClient` 是同步的，Node 版方法返回 Promise，其余断言逐条对应。
 */

import { inspect } from "node:util";

import { describe, expect, it } from "vitest";
import { ZodError } from "zod";

import {
  OidcClientError,
  OidcDeviceAuthorizationDeniedError,
  OidcDeviceAuthorizationExpiredError,
  OidcDeviceAuthorizationNotSupportedError,
  OidcDeviceClient,
  OidcDeviceClientConfigSchema,
  OidcDeviceTokenPollingStatus,
  OidcProviderUnavailableError,
  OidcTokenResponse,
} from "../../src/oidc/index.js";
import { fakeFetch, jsonResponse, textResponse, type FakeHandler } from "./helpers.js";

const ISSUER = "https://issuer.example/realms/acps-registry";
const CLIENT_ID = "registry-cli";

function discoveryPayload(
  options: {
    issuer?: string;
    includeDeviceEndpoint?: boolean;
    includeRevocationEndpoint?: boolean;
  } = {},
): Record<string, unknown> {
  const issuer = options.issuer ?? ISSUER;
  const payload: Record<string, unknown> = {
    issuer,
    jwks_uri: `${issuer}/protocol/openid-connect/certs`,
    token_endpoint: `${issuer}/protocol/openid-connect/token`,
  };
  if (options.includeDeviceEndpoint ?? true) {
    payload.device_authorization_endpoint = `${issuer}/protocol/openid-connect/auth/device`;
  }
  if (options.includeRevocationEndpoint ?? true) {
    payload.revocation_endpoint = `${issuer}/protocol/openid-connect/revoke`;
  }
  return payload;
}

function makeClient(
  handler: FakeHandler,
  options: { issuer?: string; requireHttps?: boolean; scopes?: string[] } = {},
) {
  const fetch = fakeFetch(handler);
  const client = new OidcDeviceClient(
    {
      issuer: options.issuer ?? ISSUER,
      client_id: CLIENT_ID,
      scopes: options.scopes ?? ["openid", "profile"],
      require_https: options.requireHttps ?? true,
    },
    { fetch },
  );
  return { client, fetch };
}

const isDiscovery = (path: string) => path.endsWith("/.well-known/openid-configuration");

describe("test_device_client (ported)", () => {
  it("test_device_client_config_reports_invalid_scope_types_as_validation_errors", () => {
    let error: unknown;
    try {
      OidcDeviceClientConfigSchema.parse({ issuer: ISSUER, client_id: CLIENT_ID, scopes: 123 });
    } catch (e) {
      error = e;
    }
    expect(error).toBeInstanceOf(ZodError);
    expect((error as ZodError).issues[0]?.path).toEqual(["scopes"]);
    expect((error as ZodError).issues[0]?.message).toBe(
      "scopes must be a string or iterable of strings",
    );
    expect(
      () => new OidcDeviceClient({ issuer: ISSUER, client_id: CLIENT_ID, scopes: 123 as never }),
    ).toThrow(ZodError);
  });

  it("test_start_device_authorization_fails_when_provider_lacks_endpoint", async () => {
    const { client } = makeClient(() =>
      jsonResponse(200, discoveryPayload({ includeDeviceEndpoint: false })),
    );

    const error = await client.startDeviceAuthorization().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(OidcDeviceAuthorizationNotSupportedError);
    expect((error as Error).message).toContain("device_authorization_endpoint");
  });

  it.each([true, false])(
    "test_start_device_authorization_sends_public_client_fields (complete url: %s)",
    async (includeCompleteUrl) => {
      const { client, fetch } = makeClient((request) => {
        if (isDiscovery(request.path)) {
          return jsonResponse(200, discoveryPayload());
        }
        const payload: Record<string, unknown> = {
          device_code: "device-code-123",
          user_code: "USER-CODE",
          verification_uri: "https://issuer.example/device",
          expires_in: 600,
          interval: 7,
        };
        if (includeCompleteUrl) {
          payload.verification_uri_complete = "https://issuer.example/device?user_code=USER-CODE";
        }
        return jsonResponse(200, payload);
      });

      const response = await client.startDeviceAuthorization();

      expect(response.device_code.getSecretValue()).toBe("device-code-123");
      expect(response.user_code).toBe("USER-CODE");
      expect(response.verification_uri).toBe("https://issuer.example/device");
      expect(response.verification_uri_complete).toBe(
        includeCompleteUrl ? "https://issuer.example/device?user_code=USER-CODE" : undefined,
      );
      expect(response.interval).toBe(7);
      expect(
        fetch.requests.filter((r) => !isDiscovery(r.path)).map((r) => [r.path, r.form]),
      ).toEqual([
        [
          "/realms/acps-registry/protocol/openid-connect/auth/device",
          { client_id: CLIENT_ID, scope: "openid profile" },
        ],
      ]);
    },
  );

  it.each([
    [
      400,
      { error: "authorization_pending" },
      OidcDeviceTokenPollingStatus.AUTHORIZATION_PENDING,
      7,
      null,
    ],
    [400, { error: "slow_down" }, OidcDeviceTokenPollingStatus.SLOW_DOWN, 12, null],
    [
      400,
      { error: "access_denied", error_description: "operator denied" },
      null,
      null,
      OidcDeviceAuthorizationDeniedError,
    ],
    [400, { error: "expired_token" }, null, null, OidcDeviceAuthorizationExpiredError],
    [
      200,
      {
        access_token: "access-token-123",
        token_type: "bearer",
        expires_in: 300,
        refresh_token: "refresh-token-123",
      },
      OidcDeviceTokenPollingStatus.SUCCESS,
      7,
      null,
    ],
  ] as const)(
    "test_poll_device_token_handles_oauth_responses %#",
    async (status, payload, expectedStatus, expectedInterval, expectedException) => {
      const { client, fetch } = makeClient((request) =>
        isDiscovery(request.path)
          ? jsonResponse(200, discoveryPayload())
          : jsonResponse(status, payload),
      );

      if (expectedException !== null) {
        await expect(client.pollDeviceToken("device-code-123", { interval: 7 })).rejects.toThrow(
          expectedException,
        );
      } else {
        const result = await client.pollDeviceToken("device-code-123", { interval: 7 });
        expect(result.status).toBe(expectedStatus);
        expect(result.interval).toBe(expectedInterval);
        if (expectedStatus === OidcDeviceTokenPollingStatus.SUCCESS) {
          const tokenResponse = result.token_response;
          expect(tokenResponse).toBeDefined();
          expect(OidcTokenResponse.accessTokenValue(tokenResponse!)).toBe("access-token-123");
          expect(OidcTokenResponse.refreshTokenValue(tokenResponse!)).toBe("refresh-token-123");
        } else {
          expect(result.token_response).toBeUndefined();
        }
      }

      expect(fetch.requests.filter((r) => !isDiscovery(r.path)).map((r) => r.form)).toEqual([
        {
          grant_type: "urn:ietf:params:oauth:grant-type:device_code",
          client_id: CLIENT_ID,
          device_code: "device-code-123",
        },
      ]);
    },
  );

  it("test_refresh_token_uses_public_client_form_body", async () => {
    const { client, fetch } = makeClient((request) =>
      isDiscovery(request.path)
        ? jsonResponse(200, discoveryPayload())
        : jsonResponse(200, {
            access_token: "new-access-token",
            refresh_token: "new-refresh-token",
            token_type: "Bearer",
            expires_in: 240,
            refresh_expires_in: 3600,
          }),
    );

    const response = await client.refreshToken("refresh-token-123");

    expect(OidcTokenResponse.accessTokenValue(response)).toBe("new-access-token");
    expect(OidcTokenResponse.refreshTokenValue(response)).toBe("new-refresh-token");
    expect(OidcTokenResponse.idTokenValue(response)).toBeNull();
    expect(response.token_type).toBe("Bearer");
    expect(fetch.requests.filter((r) => !isDiscovery(r.path)).map((r) => r.form)).toEqual([
      { grant_type: "refresh_token", client_id: CLIENT_ID, refresh_token: "refresh-token-123" },
    ]);
  });

  it("test_revoke_token_returns_structured_result_when_endpoint_missing", async () => {
    const { client } = makeClient(() =>
      jsonResponse(200, discoveryPayload({ includeRevocationEndpoint: false })),
    );

    const result = await client.revokeToken("refresh-token-123");

    expect(result.attempted).toBe(false);
    expect(result.revoked).toBe(false);
    expect(result.reason).toBe("revocation_endpoint_unavailable");
  });

  it("test_revoke_token_posts_without_client_secret", async () => {
    const { client, fetch } = makeClient((request) =>
      isDiscovery(request.path) ? jsonResponse(200, discoveryPayload()) : jsonResponse(200, {}),
    );

    const result = await client.revokeToken("refresh-token-123", {
      tokenTypeHint: "refresh_token",
    });

    expect(result.attempted).toBe(true);
    expect(result.revoked).toBe(true);
    expect(fetch.requests.filter((r) => !isDiscovery(r.path)).map((r) => r.form)).toEqual([
      { client_id: CLIENT_ID, token: "refresh-token-123", token_type_hint: "refresh_token" },
    ]);
  });

  it("test_require_https_rejects_non_https_issuer_by_default", async () => {
    const issuer = "http://127.0.0.1:8080/realms/acps-registry";
    const { client } = makeClient(() => jsonResponse(200, discoveryPayload({ issuer })), {
      issuer,
    });

    const error = await client.getDiscoveryDocument().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(OidcProviderUnavailableError);
    expect((error as Error).message).toContain("issuer must use https");
  });

  it("test_require_https_can_be_disabled_for_localhost", async () => {
    const issuer = "http://127.0.0.1:8080/realms/acps-registry";
    const { client } = makeClient(
      (request) =>
        isDiscovery(request.path)
          ? jsonResponse(200, discoveryPayload({ issuer }))
          : jsonResponse(200, {
              device_code: "device-code-123",
              user_code: "USER-CODE",
              verification_uri: "http://127.0.0.1:8080/device",
              expires_in: 600,
            }),
      { issuer, requireHttps: false },
    );

    const response = await client.startDeviceAuthorization();

    expect(response.user_code).toBe("USER-CODE");
  });

  it("test_public_client_requests_do_not_include_client_secret", async () => {
    const { client, fetch } = makeClient((request) => {
      if (isDiscovery(request.path)) {
        return jsonResponse(200, discoveryPayload());
      }
      if (request.path.endsWith("/auth/device")) {
        return jsonResponse(200, {
          device_code: "device-code-123",
          user_code: "USER-CODE",
          verification_uri: "https://issuer.example/device",
          expires_in: 600,
        });
      }
      if (request.path.endsWith("/token")) {
        const tokenType =
          request.form.grant_type === "urn:ietf:params:oauth:grant-type:device_code"
            ? "bearer"
            : "Bearer";
        return jsonResponse(200, { access_token: "access", token_type: tokenType });
      }
      return jsonResponse(200, {});
    });

    await client.startDeviceAuthorization();
    await client.pollDeviceToken("device-code-123");
    await client.refreshToken("refresh-token-123");
    await client.revokeToken("refresh-token-123");

    const bodies = fetch.requests.filter((r) => !isDiscovery(r.path)).map((r) => r.form);
    expect(bodies.length).toBeGreaterThan(0);
    expect(bodies.every((body) => !("client_secret" in body))).toBe(true);
  });

  it("test_token_models_and_errors_redact_sensitive_values", async () => {
    const { client } = makeClient((request) => {
      if (isDiscovery(request.path)) {
        return jsonResponse(200, discoveryPayload());
      }
      if (request.form.grant_type === "refresh_token") {
        return jsonResponse(400, { error: "invalid_grant", error_description: "refresh rejected" });
      }
      return jsonResponse(200, {
        access_token: "raw-access-token",
        refresh_token: "raw-refresh-token",
        token_type: "bearer",
      });
    });

    const error = await client.refreshToken("raw-refresh-token").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(OidcClientError);
    // str(exc) / repr(exc)
    expect(String(error)).not.toContain("raw-refresh-token");
    expect((error as Error).message).not.toContain("raw-refresh-token");
    expect(inspect(error)).not.toContain("raw-refresh-token");

    const tokenResponse = (await client.pollDeviceToken("device-code-123")).token_response;
    expect(tokenResponse).toBeDefined();
    // repr(token_response) / model_dump_json()
    expect(inspect(tokenResponse)).not.toContain("raw-access-token");
    expect(inspect(tokenResponse)).not.toContain("raw-refresh-token");
    expect(JSON.stringify(tokenResponse)).not.toContain("raw-access-token");
    expect(JSON.stringify(tokenResponse)).not.toContain("raw-refresh-token");
    expect(String(tokenResponse?.access_token)).toBe("**********");
  });
});

describe("OidcDeviceClient details (Node additions)", () => {
  it("formats OidcClientError like Python's __str__ and keeps the raw message", async () => {
    const { client } = makeClient((request) =>
      isDiscovery(request.path)
        ? jsonResponse(200, discoveryPayload())
        : jsonResponse(400, { error: " invalid_grant ", error_description: " refresh rejected " }),
    );

    const error = (await client.refreshToken("r").catch((e: unknown) => e)) as OidcClientError;
    expect(error.message).toBe(
      "OIDC refresh token request failed; error=invalid_grant; description=refresh rejected; status_code=400",
    );
    expect(error.rawMessage).toBe("OIDC refresh token request failed");
    expect(error.error).toBe("invalid_grant");
    expect(error.errorDescription).toBe("refresh rejected");
    expect(error.statusCode).toBe(400);
    expect(error.name).toBe("OidcClientError");
  });

  it("sends form-encoded bodies with Accept: application/json", async () => {
    const { client, fetch } = makeClient((request) =>
      isDiscovery(request.path)
        ? jsonResponse(200, discoveryPayload())
        : jsonResponse(400, { error: "authorization_pending" }),
    );

    await client.pollDeviceToken("code with spaces&symbols=");

    const request = fetch.requests.find((r) => !isDiscovery(r.path));
    expect(request?.method).toBe("POST");
    expect(request?.headers.get("accept")).toBe("application/json");
    expect(request?.headers.get("content-type")).toBe("application/x-www-form-urlencoded");
    expect(request?.form.device_code).toBe("code with spaces&symbols=");
  });

  it("loads the discovery document once per client", async () => {
    const { client, fetch } = makeClient((request) =>
      isDiscovery(request.path) ? jsonResponse(200, discoveryPayload()) : jsonResponse(200, {}),
    );

    await client.revokeToken("a");
    await client.revokeToken("b");

    expect(fetch.requests.filter((r) => isDiscovery(r.path))).toHaveLength(1);
  });

  it("enforces https on every advertised endpoint", async () => {
    const { client } = makeClient(() =>
      jsonResponse(200, {
        ...discoveryPayload(),
        end_session_endpoint: "http://issuer.example/logout",
      }),
    );

    await expect(client.getDiscoveryDocument()).rejects.toThrow(
      "end_session_endpoint must use https when require_https=true",
    );
  });

  it.each([
    [
      { device_code: "d", user_code: "u", verification_uri: "v" },
      "OIDC response field expires_in must be a positive integer",
    ],
    [
      { device_code: "d", user_code: "u", verification_uri: "v", expires_in: 0 },
      "OIDC response field expires_in must be a positive integer",
    ],
    [
      { device_code: "d", user_code: "u", verification_uri: "v", expires_in: 60, interval: null },
      "OIDC response field interval must be a positive integer",
    ],
    [
      { device_code: "d", verification_uri: "v", expires_in: 60 },
      "OIDC device authorization response is invalid",
    ],
  ])("validates the device authorization response %#", async (payload, message) => {
    const { client } = makeClient((request) =>
      isDiscovery(request.path)
        ? jsonResponse(200, discoveryPayload())
        : jsonResponse(200, payload),
    );

    const error = await client.startDeviceAuthorization().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(OidcClientError);
    expect((error as Error).message).toBe(message);
  });

  it("treats a boolean expires_in like Python's bool-is-int", async () => {
    const { client } = makeClient((request) =>
      isDiscovery(request.path)
        ? jsonResponse(200, discoveryPayload())
        : jsonResponse(200, {
            device_code: "d",
            user_code: "u",
            verification_uri: "v",
            expires_in: true,
          }),
    );

    const response = await client.startDeviceAuthorization();
    expect(response.expires_in).toBe(1);
    expect(response.interval).toBe(5);
    expect(response.expires_at).toBeInstanceOf(Date);
  });

  it.each([
    [
      () => textResponse(200, "not json"),
      "OIDC device authorization endpoint response is invalid; status_code=200",
    ],
    [
      () => textResponse(500, "oops"),
      "OIDC device authorization endpoint request failed; status_code=500",
    ],
    [
      () => Promise.reject(new TypeError("fetch failed")),
      `OIDC request to ${ISSUER}/protocol/openid-connect/auth/device failed`,
    ],
  ])("maps device endpoint failures %#", async (respond, message) => {
    const { client } = makeClient((request) =>
      isDiscovery(request.path) ? jsonResponse(200, discoveryPayload()) : respond(),
    );

    const error = await client.startDeviceAuthorization().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(OidcClientError);
    expect((error as Error).message).toBe(message);
  });

  it.each([
    [textResponse(200, "[]"), "OIDC token response is invalid; status_code=200"],
    [jsonResponse(200, { token_type: "mac", access_token: "a" }), "OIDC token response is invalid"],
    [
      jsonResponse(400, { error: "invalid_request" }),
      "OIDC device token polling failed; error=invalid_request; status_code=400",
    ],
    [textResponse(502, "<html>"), "OIDC device token polling failed; status_code=502"],
  ])("maps token endpoint failures %#", async (response, message) => {
    const { client } = makeClient((request) =>
      isDiscovery(request.path) ? jsonResponse(200, discoveryPayload()) : response,
    );

    const error = await client.pollDeviceToken("d").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(OidcClientError);
    expect((error as Error).message).toBe(message);
  });

  it("rejects a non-positive polling interval like the pydantic model", async () => {
    const { client } = makeClient((request) =>
      isDiscovery(request.path)
        ? jsonResponse(200, discoveryPayload())
        : jsonResponse(400, { error: "authorization_pending" }),
    );

    await expect(client.pollDeviceToken("d", { interval: 0 })).rejects.toThrow(ZodError);
  });

  it("reports a missing token endpoint and failed revocation", async () => {
    const noToken = makeClient(() =>
      jsonResponse(200, { issuer: ISSUER, jwks_uri: `${ISSUER}/certs` }),
    );
    await expect(noToken.client.refreshToken("r")).rejects.toThrow(
      "OIDC provider does not expose token_endpoint",
    );

    const failing = makeClient((request) =>
      isDiscovery(request.path) ? jsonResponse(200, discoveryPayload()) : jsonResponse(503, {}),
    );
    await expect(failing.client.revokeToken("t")).rejects.toThrow(
      "OIDC token revocation failed; status_code=503",
    );
  });
});
