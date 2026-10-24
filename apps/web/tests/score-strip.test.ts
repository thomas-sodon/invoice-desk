import { describe, expect, it } from "vitest";
import { scoreCaption } from "@/components/board/score-strip";

describe("scoreCaption", () => {
  it("never labels mock samples or offline harness runs as a benchmark", () => {
    expect(scoreCaption("benchmark on synthetic data", "mock")).toBe("sample values, not a measured run");
    expect(
      scoreCaption("benchmark on synthetic data (offline harness run, not a live model measurement)", "http"),
    ).toBe("offline harness run, not a measurement");
    expect(scoreCaption("benchmark on synthetic data", "http")).toBe("benchmark, synthetic data");
  });
});
