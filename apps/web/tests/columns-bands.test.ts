import { describe, expect, it } from "vitest";
import { DOC_STATUSES, KANBAN_COLUMNS } from "@/lib/api-types";
import { BAND_CLASSES, BAND_COLORS, bandFor } from "@/lib/bands";
import { BOARD_COLUMNS, COLUMN_LABELS, columnFor, dropActionFor, STATUS_TO_COLUMN } from "@/lib/columns";

describe("column mapping (DESIGN §4.3, invoicedesk.status.KANBAN_COLUMNS)", () => {
  it("maps every status to the documented column", () => {
    expect(STATUS_TO_COLUMN).toEqual({
      Received: "received",
      Screening: "received",
      Extracting: "received",
      Extracted: "extracted",
      Validating: "validated",
      ReadyToPay: "ready_to_pay",
      NeedsReview: "needs_review",
      Flagged: "flagged",
      Quarantined: "flagged",
      Posted: "posted",
      Rejected: "rejected",
    });
    for (const s of DOC_STATUSES) expect(KANBAN_COLUMNS).toContain(columnFor(s));
  });
  it("shows the seven board columns in order with spec labels", () => {
    expect(BOARD_COLUMNS.map((c) => COLUMN_LABELS[c])).toEqual([
      "Received",
      "Extracted",
      "Validated",
      "Ready to pay",
      "Needs review",
      "Flagged",
      "Posted",
    ]);
  });
  it("only lets drags do what a person may do", () => {
    expect(dropActionFor("ReadyToPay", "posted")).toEqual({ kind: "post" });
    expect(dropActionFor("NeedsReview", "reject-tray")).toEqual({ kind: "reject" });
    expect(dropActionFor("Flagged", "reject-tray")).toEqual({ kind: "reject" });
    expect(dropActionFor("Flagged", "ready_to_pay")).toEqual({ kind: "approve" });
    expect(dropActionFor("Flagged", "posted").kind).toBe("refuse");
    expect(dropActionFor("NeedsReview", "ready_to_pay").kind).toBe("refuse");
    expect(dropActionFor("Received", "ready_to_pay").kind).toBe("refuse");
    expect(dropActionFor("ReadyToPay", "reject-tray").kind).toBe("refuse");
    expect(dropActionFor("Posted", "posted")).toEqual({ kind: "refuse", reason: "" });
  });
});

describe("confidence bands", () => {
  it("green at or above the tenant threshold, amber between, red below 0.5", () => {
    expect(bandFor(0.95, 0.9)).toBe("green");
    expect(bandFor(0.9, 0.9)).toBe("green");
    expect(bandFor(0.89, 0.9)).toBe("amber");
    expect(bandFor(0.5, 0.9)).toBe("amber");
    expect(bandFor(0.49, 0.9)).toBe("red");
    expect(bandFor(null, 0.9)).toBe("red");
    expect(bandFor(Number.NaN, 0.9)).toBe("red");
  });
  it("a failed rule makes the field red regardless of confidence", () => {
    expect(bandFor(0.99, 0.9, true)).toBe("red");
  });
  it("has distinct green/amber/red colors", () => {
    expect(BAND_COLORS.green.stroke).toBe("#15803d");
    expect(BAND_COLORS.amber.stroke).toBe("#b45309");
    expect(BAND_COLORS.red.stroke).toBe("#b91c1c");
    expect(new Set(Object.values(BAND_COLORS).map((c) => c.stroke)).size).toBe(3);
    expect(BAND_CLASSES.green).toContain("green");
    expect(BAND_CLASSES.amber).toContain("amber");
    expect(BAND_CLASSES.red).toContain("red");
  });
});
