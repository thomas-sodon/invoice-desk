import { describe, expect, it } from "vitest";
import { addUsd, compareUsd, formatCents, formatUsd, formatUsdMeter, parseDollarsToCents } from "@/lib/money";

describe("formatCents", () => {
  it("formats integer cents with separators", () => {
    expect(formatCents(124000)).toBe("$1,240.00");
    expect(formatCents(34000)).toBe("$340.00");
    expect(formatCents(5)).toBe("$0.05");
    expect(formatCents(0)).toBe("$0.00");
    expect(formatCents(123456789)).toBe("$1,234,567.89");
  });
  it("handles negatives, null and dropZeroCents", () => {
    expect(formatCents(-1250)).toBe("-$12.50");
    expect(formatCents(null)).toBe("—");
    expect(formatCents(undefined)).toBe("—");
    expect(formatCents(256000, { dropZeroCents: true })).toBe("$2,560");
    expect(formatCents(256001, { dropZeroCents: true })).toBe("$2,560.01");
  });
  it("refuses floats", () => {
    expect(() => formatCents(12.5)).toThrow(TypeError);
  });
  it("does not lose precision at large values", () => {
    expect(formatCents(Number.MAX_SAFE_INTEGER)).toBe("$90,071,992,547,409.91");
  });
});

describe("parseDollarsToCents", () => {
  it("parses typed amounts without floating point", () => {
    expect(parseDollarsToCents("1,240.00")).toBe(124000);
    expect(parseDollarsToCents("$1240")).toBe(124000);
    expect(parseDollarsToCents("0.1")).toBe(10);
    expect(parseDollarsToCents("0.29")).toBe(29);
    expect(parseDollarsToCents(" 19.99 ")).toBe(1999);
    expect(parseDollarsToCents("-5.25")).toBe(-525);
  });
  it("rejects junk and sub-cent precision", () => {
    expect(parseDollarsToCents("")).toBeNull();
    expect(parseDollarsToCents("abc")).toBeNull();
    expect(parseDollarsToCents("1.234")).toBeNull();
    expect(parseDollarsToCents("1e5")).toBeNull();
    expect(parseDollarsToCents("99999999999999999")).toBeNull();
  });
  it("round-trips with formatCents", () => {
    for (const c of [0, 1, 99, 100, 124000, 379665]) expect(parseDollarsToCents(formatCents(c))).toBe(c);
  });
});

describe("USD strings", () => {
  it("formats decimal strings", () => {
    expect(formatUsd("0.0112", 4)).toBe("$0.0112");
    expect(formatUsd("0.011", 3)).toBe("$0.011");
    expect(formatUsd("0.27")).toBe("$0.27");
    expect(formatUsd("12")).toBe("$12.00");
    expect(formatUsd("0.10000000")).toBe("$0.10");
    expect(formatUsd(null)).toBe("—");
    expect(formatUsd("not a number")).toBe("—");
  });
  it("adds exactly", () => {
    expect(addUsd("0.1", "0.2")).toBe("0.3");
    expect(addUsd("0.0101", "0.0101", null, undefined, "0.0011")).toBe("0.0213");
    expect(addUsd()).toBe("0");
    expect(addUsd("-0.5", "0.25")).toBe("-0.25");
    expect(compareUsd("0.27", "0.2700")).toBe(0);
    expect(compareUsd("0.1", "0.2")).toBe(-1);
  });
  it("formats the cost meter", () => {
    expect(formatUsdMeter("0.2705")).toBe("$0.27");
    expect(formatUsdMeter("0.275")).toBe("$0.28");
    expect(formatUsdMeter("0.0101")).toBe("$0.0101");
    expect(formatUsdMeter("0")).toBe("$0.00");
    expect(formatUsdMeter(null)).toBe("$0.00");
    expect(formatUsdMeter("12.3456")).toBe("$12.35");
  });
});
