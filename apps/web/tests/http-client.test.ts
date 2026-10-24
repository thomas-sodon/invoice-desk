import { describe, expect, it, vi } from "vitest";
import { ApiError, isMockMode } from "@/lib/api";
import { setDevUser } from "@/lib/auth";
import { HttpApiClient } from "@/lib/http-client";
import fixtures from "@/lib/mock/fixtures/demo-batch.json";

function respond(status: number, body: unknown) {
  return new Response(body === undefined ? "" : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("mode selection", () => {
  it("uses mock without a base URL or when forced", () => {
    expect(isMockMode({})).toBe(true);
    expect(isMockMode({ NEXT_PUBLIC_API_BASE: "http://api" })).toBe(false);
    expect(isMockMode({ NEXT_PUBLIC_API_BASE: "http://api", NEXT_PUBLIC_API_MOCK: "1" })).toBe(true);
    expect(isMockMode({ NEXT_PUBLIC_API_MOCK: "0" })).toBe(false);
  });
});

describe("HttpApiClient", () => {
  it("builds /api/v1 URLs with query params and dev auth", async () => {
    setDevUser("dana.clerk@maplerow.example");
    const fetchImpl = vi.fn(async () => respond(200, { items: [], next_cursor: null }));
    const api = new HttpApiClient("http://api.local/", fetchImpl as unknown as typeof fetch);
    await api.listDocuments({ column: "flagged", limit: 5 });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://api.local/api/v1/documents?column=flagged&limit=5");
    expect((init.headers as Record<string, string>)["X-Dev-User"]).toBe("dana.clerk@maplerow.example");
    expect(init.credentials).toBe("include");
  });

  it("sends corrections and approvals as JSON bodies", async () => {
    const fetchImpl = vi.fn(async () => respond(200, { status: "Validating", approvals: [] }));
    const api = new HttpApiClient("http://api", fetchImpl as unknown as typeof fetch);
    await api.correct("d 1", [{ field: "total", value: "124000" }]);
    await api.approve("d1", { kind: "bank_change", decision: "approve", callback_attested: true });
    const calls = fetchImpl.mock.calls as unknown as [string, RequestInit][];
    expect(calls[0]?.[0]).toBe("http://api/api/v1/documents/d%201/corrections");
    expect(JSON.parse(String(calls[0]?.[1].body))).toEqual({ corrections: [{ field: "total", value: "124000" }] });
    expect(calls[1]?.[1].method).toBe("POST");
    expect(JSON.parse(String(calls[1]?.[1].body))).toMatchObject({ kind: "bank_change", callback_attested: true });
  });

  it("maps contract errors to ApiError", async () => {
    const api = new HttpApiClient("http://api", (async () =>
      respond(403, { error: { code: "self_approval", message: "You uploaded this." } })) as unknown as typeof fetch);
    const err = await api.approve("d", { kind: "bank_change", decision: "approve", callback_attested: true }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 403, code: "self_approval", message: "You uploaded this." });
  });

  it("validates document detail responses", async () => {
    const doc = (fixtures as unknown as { documents: unknown[] }).documents[0];
    const ok = new HttpApiClient("http://api", (async () => respond(200, doc)) as unknown as typeof fetch);
    expect((await ok.getDocument("x")).filename).toMatch(/^01-/);
    const bad = new HttpApiClient("http://api", (async () => respond(200, { id: "x" })) as unknown as typeof fetch);
    await expect(bad.getDocument("x")).rejects.toMatchObject({ code: "bad_response" });
  });

  it("returns null for a missing scorecard and survives network failure", async () => {
    const api = new HttpApiClient("http://api", (async () => respond(404, { error: { code: "not_found", message: "none" } })) as unknown as typeof fetch);
    expect(await api.scorecard()).toBeNull();
    expect(await api.me()).toBeNull();
    const down = new HttpApiClient("http://api", (async () => {
      throw new TypeError("fetch failed");
    }) as unknown as typeof fetch);
    await expect(down.dashboard()).rejects.toMatchObject({ code: "network" });
  });

  it("streams SSE board events", async () => {
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        const enc = new TextEncoder();
        c.enqueue(enc.encode(": keepalive\n\n"));
        c.enqueue(enc.encode('data: {"type":"document.status_changed","tenant_id":"maple_row","document_id":"d","status":"Flagged","column":"flagged","ts":"t"}\n\n'));
        c.close();
      },
    });
    let calls = 0;
    const fetchImpl = vi.fn(async () => {
      calls += 1;
      if (calls > 1) return new Promise<Response>(() => undefined);
      return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    });
    const api = new HttpApiClient("http://api", fetchImpl as unknown as typeof fetch);
    const got: string[] = [];
    const sub = api.subscribe("b1", (e) => got.push(`${e.document_id}:${e.column}`));
    await vi.waitFor(() => expect(got).toEqual(["d:flagged"]));
    expect((fetchImpl.mock.calls[0] as unknown as [string])[0]).toBe("http://api/api/v1/events?batch_id=b1");
    sub.close();
  });

  it("points the report card at the API", () => {
    expect(new HttpApiClient("http://api").reportCardUrl()).toBe("http://api/api/v1/report-card.pdf");
  });
});
