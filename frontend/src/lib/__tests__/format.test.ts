import { describe, it, expect } from "vitest";
import {
  formatUptime,
  formatTimestamp,
  middleTruncate,
  relativeTime,
  restoreCommand,
} from "../format";

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

describe("relativeTime", () => {
  const now = Date.parse("2026-06-09T12:00:00Z");

  it("formats seconds, minutes, hours, days", () => {
    expect(relativeTime("2026-06-09T11:59:55Z", now)).toBe("5 seconds ago");
    expect(relativeTime("2026-06-09T11:59:59Z", now)).toBe("1 second ago");
    expect(relativeTime("2026-06-09T11:58:00Z", now)).toBe("2 minutes ago");
    expect(relativeTime("2026-06-09T09:00:00Z", now)).toBe("3 hours ago");
    expect(relativeTime("2026-06-07T12:00:00Z", now)).toBe("2 days ago");
  });

  it("returns null for missing or invalid input", () => {
    expect(relativeTime(null, now)).toBeNull();
    expect(relativeTime("not-a-date", now)).toBeNull();
  });
});

describe("middleTruncate", () => {
  it("leaves short strings alone", () => {
    expect(middleTruncate("demo.db", 20)).toBe("demo.db");
  });

  it("truncates in the middle at the requested length", () => {
    const result = middleTruncate("/Users/alex/work/simonw/demo/backups/demo.db", 21);
    expect(result).toBe("/Users/ale…ps/demo.db");
    expect(result.length).toBe(21);
  });
});

describe("restoreCommand", () => {
  it("quotes plain paths and URLs", () => {
    expect(restoreCommand("/tmp/data.db", "s3://bucket/data")).toBe(
      "litestream restore -o 'data.db' 's3://bucket/data'",
    );
  });

  it("keeps filenames with spaces as a single shell word", () => {
    expect(restoreCommand("/tmp/my data.db", "s3://b/x")).toBe(
      "litestream restore -o 'my data.db' 's3://b/x'",
    );
  });

  it("cannot be escaped by single quotes in the replica URL", () => {
    // A manage-permission user can register a URL like this; the copy-paste
    // command must treat it as data, not shell.
    const cmd = restoreCommand("/tmp/x.db", "file:///tmp/x'$(rm -rf ~)'");
    expect(cmd).toBe(
      "litestream restore -o 'x.db' 'file:///tmp/x'\\''$(rm -rf ~)'\\'''",
    );
  });
});
