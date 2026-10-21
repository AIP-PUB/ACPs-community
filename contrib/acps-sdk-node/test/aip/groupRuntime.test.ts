import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AsyncEvent,
  AsyncLock,
  CancelledError,
  DEFAULT_INVITATION_TIMEOUT_SECONDS,
  Deferred,
  INBOX_EXCHANGE_NAME,
  INBOX_MESSAGE_TTL_MS,
  INBOX_QUEUE_EXPIRES_MS,
  INVITATION_CLOCK_SKEW_SECONDS,
  JSONDecodeError,
  buildExternalConnectionUrl,
  buildGroupExchangeName,
  buildGroupQueueName,
  buildInboxQueueName,
  buildPlainConnectionUrl,
  calculateInvitationExpiry,
  createInvitationToken,
  createTask,
  decodeJsonBody,
  getEndpointUrl,
  invitationIsExpired,
  normalizeGroupId,
  parseAmqpEndpointUrl,
  parseUtcDatetime,
  pyQuote,
  pyUnquote,
  replaceAicPlaceholder,
  requireJsonDict,
  sleep,
} from "../../src/aip/groupRuntime.js";
import { pyRepr } from "../../src/internal/pyCompat.js";
import { toPythonIsoformat } from "../../src/internal/time.js";
import { loadPythonFixture } from "../fixtures.js";

const LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4";
const PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JUE";

interface RuntimeFixture {
  constants: Record<string, string | number>;
  names: Record<string, string>;
  normalize_group_id: Array<{ input: string; output?: string; error?: string }>;
  plain_urls: Array<{
    input: {
      host: string;
      port: number;
      vhost: string;
      username: string;
      password: string;
      connection_name: string | null;
    };
    output: string;
  }>;
  external_urls: Array<{
    input: { host: string; port: number; vhost: string; connection_name: string };
    output: string;
  }>;
  endpoints: Array<{
    url: string;
    aic: string | null;
    output?: Record<string, unknown>;
    error?: string;
  }>;
  get_endpoint_url: {
    acs_data: Record<string, unknown>;
    amqp: string | null;
    jsonrpc: string | null;
    missing: string | null;
    not_dict: string | null;
    no_list: string | null;
  };
  parse_utc_datetime: Array<{ input: string; output?: string; error?: string }>;
  invitation_expiry_300: string;
  now: string;
  expired: Record<string, boolean>;
}

const fixture = loadPythonFixture<RuntimeFixture>("aip_group", "runtime");

afterEach(() => {
  vi.useRealTimers();
});

// 对应 tests/test_group_runtime.py
describe("test_group_runtime", () => {
  it("test_group_name_builders_follow_v21_convention", () => {
    const exchange = buildGroupExchangeName(LEADER_AIC, "group-session-1");
    const queue = buildGroupQueueName(LEADER_AIC, "group-session-1", PARTNER_AIC);

    expect(exchange).toBe(`group_${LEADER_AIC}_group-session-1`);
    expect(queue).toBe(`group_${LEADER_AIC}_group-session-1_${PARTNER_AIC}`);
    expect(buildInboxQueueName(PARTNER_AIC)).toBe(`inbox_${PARTNER_AIC}`);
  });

  it("test_parse_amqp_endpoint_url_replaces_aic_placeholder", () => {
    const endpoint = parseAmqpEndpointUrl(
      "amqps://mq.acps.example.com:5671/acps?inbox=inbox_{AIC}",
      {
        aic: PARTNER_AIC,
      },
    );

    expect(endpoint.host).toBe("mq.acps.example.com");
    expect(endpoint.port).toBe(5671);
    expect(endpoint.vhost).toBe("acps");
    expect(endpoint.inbox).toBe(`inbox_${PARTNER_AIC}`);
  });

  it("test_normalize_group_id_replaces_invalid_session_separators", () => {
    expect(normalizeGroupId("group-sess_b50a1a6a8fb24024")).toBe("group-sess-b50a1a6a8fb24024");
  });
});

describe("Python fixtures: aip_group/runtime", () => {
  it("exports the same constants", () => {
    expect({
      INBOX_EXCHANGE_NAME,
      INBOX_QUEUE_EXPIRES_MS,
      INBOX_MESSAGE_TTL_MS,
      DEFAULT_INVITATION_TIMEOUT_SECONDS,
      INVITATION_CLOCK_SKEW_SECONDS,
    }).toEqual(fixture.constants);
  });

  it("builds the same names and errors", () => {
    const names = fixture.names;
    const fixtureLeader = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4";
    const fixturePartner = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5";
    expect(buildGroupExchangeName(fixtureLeader, "group-session-1")).toBe(names.exchange);
    expect(buildGroupQueueName(fixtureLeader, "group-session-1", fixturePartner)).toBe(names.queue);
    expect(buildInboxQueueName(fixturePartner)).toBe(names.inbox);
    expect(() => buildInboxQueueName("leader-aic")).toThrow(names.invalid_aic);
    expect(() => buildGroupExchangeName(fixtureLeader, "bad_group")).toThrow(names.invalid_group);
    expect(() => replaceAicPlaceholder("x{AIC}y{AIC}", null)).toThrow(
      names.placeholder_missing_aic,
    );
    expect(replaceAicPlaceholder("x{AIC}y{AIC}", fixtureLeader)).toBe(names.placeholder_replaced);
  });

  it("normalizes group ids identically", () => {
    for (const testCase of fixture.normalize_group_id) {
      if (testCase.error !== undefined) {
        expect(() => normalizeGroupId(testCase.input), testCase.input).toThrow(testCase.error);
      } else {
        expect(normalizeGroupId(testCase.input), testCase.input).toBe(testCase.output);
      }
    }
  });

  it("builds byte-identical connection URLs", () => {
    for (const { input, output } of fixture.plain_urls) {
      expect(
        buildPlainConnectionUrl({
          host: input.host,
          port: input.port,
          vhost: input.vhost,
          username: input.username,
          password: input.password,
          connectionName: input.connection_name,
        }),
      ).toBe(output);
    }
    for (const { input, output } of fixture.external_urls) {
      expect(
        buildExternalConnectionUrl({
          host: input.host,
          port: input.port,
          vhost: input.vhost,
          connectionName: input.connection_name,
        }),
      ).toBe(output);
    }
  });

  it("parses AMQP endpoint URLs like urllib.parse", () => {
    for (const testCase of fixture.endpoints) {
      const label = `${testCase.url} aic=${testCase.aic}`;
      if (testCase.error !== undefined) {
        expect(() => parseAmqpEndpointUrl(testCase.url, { aic: testCase.aic }), label).toThrow(
          testCase.error,
        );
      } else {
        expect({ ...parseAmqpEndpointUrl(testCase.url, { aic: testCase.aic }) }, label).toEqual(
          testCase.output,
        );
      }
    }
  });

  it("finds endpoint URLs in ACS data", () => {
    const data = fixture.get_endpoint_url;
    const fixturePartner = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5";
    expect(getEndpointUrl(data.acs_data, "AMQP", { aic: fixturePartner })).toBe(data.amqp);
    expect(getEndpointUrl(data.acs_data, "jsonrpc")).toBe(data.jsonrpc);
    expect(getEndpointUrl(data.acs_data, "HTTP")).toBe(data.missing);
    expect(getEndpointUrl(null, "AMQP")).toBe(data.not_dict);
    expect(getEndpointUrl({ endPoints: {} }, "AMQP")).toBe(data.no_list);
  });

  it("parses timestamps like datetime.fromisoformat", () => {
    for (const testCase of fixture.parse_utc_datetime) {
      if (testCase.error !== undefined) {
        expect(() => parseUtcDatetime(testCase.input), testCase.input).toThrow(testCase.error);
      } else {
        // JS Date 只有毫秒精度：Python 结果的微秒部分截断到毫秒后比较
        const expected = String(testCase.output).replace(/\.(\d{3})(\d{3})/, (_m, ms: string) =>
          ms === "000" ? "" : `.${ms}000`,
        );
        expect(toPythonIsoformat(parseUtcDatetime(testCase.input)), testCase.input).toBe(expected);
      }
    }
  });

  it("computes invitation expiry and expiration with clock skew", () => {
    vi.useFakeTimers({ toFake: ["Date"], now: new Date(fixture.now) });
    expect(calculateInvitationExpiry(300)).toBe(fixture.invitation_expiry_300);
    for (const [expiresAt, expected] of Object.entries(fixture.expired)) {
      expect(invitationIsExpired(expiresAt), expiresAt).toBe(expected);
    }
  });
});

describe("python string helpers", () => {
  it("quote / unquote follow urllib semantics", () => {
    expect(pyQuote("a b/c!*'()~-_.")).toBe("a%20b%2Fc%21%2A%27%28%29~-_.");
    expect(pyQuote("中")).toBe("%E4%B8%AD");
    expect(pyUnquote("%E4%B8%AD%zz%4")).toBe("中%zz%4");
    expect(pyUnquote("%ff")).toBe("�");
  });

  it("createInvitationToken matches secrets.token_urlsafe(32)", () => {
    const token = createInvitationToken();
    expect(token).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });

  it("decodeJsonBody distinguishes JSON errors from UTF-8 errors", () => {
    expect(decodeJsonBody(Buffer.from('{"a": 1}'))).toEqual({ a: 1 });
    expect(() => decodeJsonBody(Buffer.from("not json"))).toThrow(JSONDecodeError);
    expect(() => decodeJsonBody(Buffer.from([0xff, 0xfe]))).toThrow(TypeError);
    expect(() => requireJsonDict([1])).toThrow("'list' object has no attribute 'get'");
    expect(pyRepr({ a: [1, "x", null, true], "b'": "it's" })).toBe(
      `{'a': [1, 'x', None, True], "b'": "it's"}`,
    );
  });
});

describe("asyncio helpers", () => {
  it("Deferred mirrors asyncio.Future", async () => {
    const future = new Deferred<number>();
    expect(future.done()).toBe(false);
    expect(() => future.exception()).toThrow();
    future.setResult(3);
    expect(future.done()).toBe(true);
    expect(future.exception()).toBeNull();
    await expect(future.promise).resolves.toBe(3);
    expect(() => future.setResult(4)).toThrow();

    const failed = new Deferred<number>();
    const error = new Error("boom");
    failed.setException(error); // 无人等待也不会触发 unhandledRejection
    expect(failed.exception()).toBe(error);
    await expect(failed.promise).rejects.toBe(error);
  });

  it("AsyncEvent / AsyncLock behave like asyncio primitives", async () => {
    const event = new AsyncEvent();
    const waited = event.wait();
    expect(event.isSet()).toBe(false);
    event.set();
    await expect(waited).resolves.toBe(true);
    event.clear();
    expect(event.isSet()).toBe(false);

    const lock = new AsyncLock();
    const order: string[] = [];
    await Promise.all([
      lock.runExclusive(async () => {
        order.push("a-start");
        await sleep(5);
        order.push("a-end");
      }),
      lock.runExclusive(async () => {
        order.push("b");
      }),
    ]);
    expect(order).toEqual(["a-start", "a-end", "b"]);
    expect(lock.locked()).toBe(false);
  });

  it("createTask cancellation raises CancelledError like asyncio.Task.cancel", async () => {
    const task = createTask((signal) => sleep(3_600_000, signal));
    await Promise.resolve();
    expect(task.done()).toBe(false);
    expect(task.cancel()).toBe(true);
    await expect(task.promise).rejects.toBeInstanceOf(CancelledError);
    expect(task.cancelled()).toBe(true);
    expect(task.cancel()).toBe(false);

    const completed = createTask(async () => undefined);
    await completed.promise;
    expect(completed.done()).toBe(true);
    expect(completed.cancelled()).toBe(false);
  });
});
