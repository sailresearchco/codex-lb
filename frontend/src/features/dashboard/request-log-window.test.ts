import { describe, expect, it } from "vitest";

import { isRequestLogRangeValid, requestLogTimestamp } from "./request-log-window";

describe("request log time bounds", () => {
  it("preserves microseconds while normalizing offset timestamps", () => {
    expect(requestLogTimestamp("2020-01-01T02:00:00.000001+02:00")).toBe("2020-01-01T00:00:00.000001Z");
    expect(isRequestLogRangeValid("2020-01-01T00:00:00Z", "2020-01-08T00:00:00.000001Z")).toBe(false);
    expect(isRequestLogRangeValid("2020-01-01T00:00:00.000001Z", "2020-01-08T00:00:00.000001Z")).toBe(true);
  });

  it.each(["2020-02-30T00:00:00Z", "2021-02-29T00:00:00Z", "2020-01-01T24:00:00Z", "2020-01-01T00:00:00+24:00"])(
    "rejects invalid calendar or offset input %s",
    (value) => {
      expect(requestLogTimestamp(value)).toBeUndefined();
      expect(isRequestLogRangeValid(value, "2020-03-01T00:00:00Z")).toBe(false);
    },
  );
});
