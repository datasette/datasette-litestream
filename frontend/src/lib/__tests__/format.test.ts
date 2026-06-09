import { describe, it, expect } from "vitest";
import { formatUptime, formatTimestamp } from "../format";

describe("formatUptime", () => {
  it("formats seconds, minutes, hours, days", () => {
    expect(formatUptime(0)).toBe("0s");
    expect(formatUptime(45)).toBe("45s");
    expect(formatUptime(125)).toBe("2m 5s");
    expect(formatUptime(3700)).toBe("1h 1m");
    expect(formatUptime(90000)).toBe("1d 1h 0m");
  });

  it("handles undefined", () => {
    expect(formatUptime(undefined)).toBe("—");
  });
});

describe("formatTimestamp", () => {
  it("returns dash for null/empty", () => {
    expect(formatTimestamp(null)).toBe("—");
    expect(formatTimestamp(undefined)).toBe("—");
  });

  it("passes through unparseable values", () => {
    expect(formatTimestamp("not-a-date")).toBe("not-a-date");
  });
});
