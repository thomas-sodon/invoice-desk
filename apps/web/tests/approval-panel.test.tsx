import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ApiProvider } from "@/components/api-provider";
import { ApprovalPanel } from "@/components/viewer/approval-panel";
import { HiddenTextAlert } from "@/components/viewer/document-viewer";
import { setApiForTests } from "@/lib/api";
import type { DocumentDetail } from "@/lib/api-types";
import { setDevUser } from "@/lib/auth";
import { MOCK_STAFF, MockApiClient } from "@/lib/mock/server";
import fixtures from "@/lib/mock/fixtures/demo-batch.json";

const docs = (fixtures as unknown as { documents: DocumentDetail[] }).documents;
const attack = docs.find((d) => d.filename.startsWith("18-")) as DocumentDetail;

async function renderAs(email: string, doc: DocumentDetail) {
  setDevUser(email);
  setApiForTests(new MockApiClient({ persist: false, latencyMs: 0 }));
  await act(async () => {
    render(
      <ApiProvider>
        <ApprovalPanel doc={doc} onDone={() => undefined} />
      </ApiProvider>,
    );
  });
}

afterEach(() => {
  cleanup();
  setApiForTests(null);
});

describe("ApprovalPanel", () => {
  it("hides approve from the uploader", async () => {
    const priya = MOCK_STAFF.find((s) => s.role === "approver");
    await renderAs("priya.approver@maplerow.example", { ...attack, uploaded_by: priya?.id ?? null });
    await waitFor(() => expect(screen.getByTestId("approve-blocked").textContent).toMatch(/You uploaded this invoice/));
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("hides approve from clerks", async () => {
    await renderAs("dana.clerk@maplerow.example", attack);
    await waitFor(() => expect(screen.getByTestId("approve-blocked").textContent).toMatch(/approver or the owner/));
  });

  it("requires the callback box before approving a bank change", async () => {
    await renderAs("sam.owner@maplerow.example", attack);
    const approve = await screen.findByRole("button", { name: "Approve" });
    expect((approve as HTMLButtonElement).disabled).toBe(true);
    await act(async () => screen.getByRole("checkbox").click());
    expect((approve as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText(/0 of 2 approvals/)).toBeTruthy();
    // bank number shown masked only
    expect(document.body.textContent).not.toMatch(/\d{6,}/);
  });
});

describe("HiddenTextAlert", () => {
  it("shows the demo wording with masked numbers", () => {
    render(<HiddenTextAlert doc={attack} />);
    const alert = screen.getByTestId("hidden-text-alert");
    expect(alert.textContent).toContain("Hidden instruction detected: attempted bank-account change, blocked");
    expect(alert.textContent).not.toMatch(/\d{6,}/);
    expect(alert.textContent).toContain("••••1234");
  });
});
