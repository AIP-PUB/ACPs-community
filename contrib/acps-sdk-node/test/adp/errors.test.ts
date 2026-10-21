import { describe, expect, it } from "vitest";

import {
  ADP_ERROR_HTTP_STATUS,
  ADP_ERROR_NAMES,
  ADPError,
  ADPErrorCode,
  ADPErrorCodeSchema,
  getHttpStatusForError,
  isADPErrorCode,
  isClientErrorCode,
  isForwardErrorCode,
  isRedirectErrorCode,
  isRetryableErrorCode,
  makeErrorResponse,
} from "../../src/adp/index.js";
import { loadPythonFixture } from "../fixtures.js";

interface Classification {
  isRedirect: boolean;
  isRetryable: boolean;
  isClientError: boolean;
  isForwardError: boolean;
}

interface ErrorsFixture {
  codes: Record<string, number>;
  classification: Record<string, Classification>;
  names: Record<string, string>;
  httpStatus: Record<string, number>;
  adpErrors: (Classification & {
    name: string;
    args: { code: number; message: string | null; data: unknown };
    message: string;
    str: string;
    httpStatus: number;
    errorBody: unknown;
    responseDict: unknown;
  })[];
  makeErrorResponse: {
    args: { code: number; message: string | null; data: unknown };
    result: unknown;
  }[];
  getHttpStatusForError: { code: number; status: number }[];
}

const fixture = loadPythonFixture<ErrorsFixture>("adp", "errors");

describe("Python fixtures: ADPErrorCode", () => {
  it("defines the same members and values", () => {
    expect({ ...ADPErrorCode }).toEqual(fixture.codes);
  });

  it.each(Object.entries(fixture.classification))("classifies %s", (name, expected) => {
    const code = ADPErrorCode[name as keyof typeof ADPErrorCode];
    expect({
      isRedirect: isRedirectErrorCode(code),
      isRetryable: isRetryableErrorCode(code),
      isClientError: isClientErrorCode(code),
      isForwardError: isForwardErrorCode(code),
    }).toEqual(expected);
  });

  it("maps codes to names and HTTP status like Python", () => {
    expect({ ...ADP_ERROR_NAMES }).toEqual(fixture.names);
    expect({ ...ADP_ERROR_HTTP_STATUS }).toEqual(fixture.httpStatus);
  });

  it("ADPErrorCodeSchema accepts only defined codes", () => {
    expect(ADPErrorCodeSchema.safeParse(40001).success).toBe(true);
    expect(ADPErrorCodeSchema.safeParse(40099).success).toBe(false);
    expect(ADPErrorCodeSchema.safeParse("MISSING_QUERY").success).toBe(false);
    expect(ADPErrorCodeSchema.safeParse("40001").success).toBe(false);
    expect(isADPErrorCode(50805)).toBe(true);
    expect(isADPErrorCode(40099)).toBe(false);
  });

  it("classifies unknown codes as false (like ADPError on ValueError)", () => {
    for (const code of [40099, 50899, 30799, 42999]) {
      expect(isRedirectErrorCode(code)).toBe(false);
      expect(isRetryableErrorCode(code)).toBe(false);
      expect(isClientErrorCode(code)).toBe(false);
      expect(isForwardErrorCode(code)).toBe(false);
    }
  });
});

describe("Python fixtures: ADPError", () => {
  it.each(fixture.adpErrors.map((item) => [item.name, item] as const))("%s", (_, item) => {
    const error = new ADPError(item.args.code, item.args.message, item.args.data);
    expect(error).toBeInstanceOf(Error);
    expect(error.name).toBe("ADPError");
    expect(error.code).toBe(item.args.code);
    expect(error.message).toBe(item.message);
    expect(error.message).toBe(item.str);
    expect(error.httpStatus).toBe(item.httpStatus);
    expect(error.toErrorBody()).toEqual(item.errorBody);
    expect(error.toResponseDict()).toEqual(item.responseDict);
    expect({
      isRedirect: error.isRedirect(),
      isRetryable: error.isRetryable(),
      isClientError: error.isClientError(),
      isForwardError: error.isForwardError(),
    }).toEqual({
      isRedirect: item.isRedirect,
      isRetryable: item.isRetryable,
      isClientError: item.isClientError,
      isForwardError: item.isForwardError,
    });
  });

  it("can be thrown and caught", () => {
    expect(() => {
      throw new ADPError(ADPErrorCode.FORWARD_LOOP_DETECTED, "检测到转发环路", {
        forwardChain: ["AIC-A", "AIC-B", "AIC-A"],
      });
    }).toThrow(expect.objectContaining({ httpStatus: 508, message: "检测到转发环路" }));
  });
});

describe("Python fixtures: make_error_response / get_http_status_for_error", () => {
  it.each(fixture.makeErrorResponse.map((item) => [item.args.code, item] as const))(
    "makeErrorResponse(%s)",
    (_, item) => {
      expect(makeErrorResponse(item.args.code, item.args.message, item.args.data)).toEqual(
        item.result,
      );
    },
  );

  it.each(fixture.getHttpStatusForError.map((item) => [item.code, item.status] as const))(
    "getHttpStatusForError(%s) = %s",
    (code, status) => {
      expect(getHttpStatusForError(code)).toBe(status);
    },
  );

  it("does not resolve inherited keys as error codes", () => {
    expect(getHttpStatusForError(Number.NaN)).toBe(500);
    expect(new ADPError(Number.NaN).message).toBe("UnknownError");
  });
});
