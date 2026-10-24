/**
 * Minimal Server-Sent Events reader over fetch (EventSource cannot send auth headers).
 * `SseParser` is pure and unit-tested; `openEventStream` adds reconnect with backoff.
 */
import { isBoardEvent, normalizeBoardEvent, type BoardEvent } from "./api-types";

export interface SseMessage {
  event: string;
  data: string;
  id: string | null;
}

export class SseParser {
  private buffer = "";
  private data: string[] = [];
  private event = "message";
  private id: string | null = null;

  /** Feeds a chunk and returns every complete message it finished. Comments are dropped. */
  push(chunk: string): SseMessage[] {
    this.buffer += chunk;
    const out: SseMessage[] = [];
    let nl: number;
    while ((nl = this.buffer.search(/\r\n|\r|\n/)) >= 0) {
      const line = this.buffer.slice(0, nl);
      const sepLen = this.buffer.startsWith("\r\n", nl) ? 2 : 1;
      this.buffer = this.buffer.slice(nl + sepLen);
      if (line === "") {
        if (this.data.length) out.push({ event: this.event, data: this.data.join("\n"), id: this.id });
        this.data = [];
        this.event = "message";
        continue;
      }
      if (line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "data") this.data.push(value);
      else if (field === "event") this.event = value;
      else if (field === "id") this.id = value;
    }
    return out;
  }
}

/** Parses one SSE data payload into a BoardEvent, or null if it does not match the contract. */
export function parseBoardEvent(data: string): BoardEvent | null {
  try {
    const v: unknown = JSON.parse(data);
    return isBoardEvent(v) ? normalizeBoardEvent(v) : null;
  } catch {
    return null;
  }
}

export interface StreamOptions {
  url: string;
  headers: Record<string, string>;
  onEvent: (e: BoardEvent) => void;
  onState?: (s: "connecting" | "live" | "reconnecting") => void;
  fetchImpl?: typeof fetch;
}

export function openEventStream(opts: StreamOptions): { close(): void } {
  const fetchImpl = opts.fetchImpl ?? fetch;
  let closed = false;
  let controller: AbortController | null = null;
  let attempt = 0;

  const run = async (): Promise<void> => {
    while (!closed) {
      controller = new AbortController();
      opts.onState?.(attempt === 0 ? "connecting" : "reconnecting");
      try {
        const res = await fetchImpl(opts.url, {
          headers: { Accept: "text/event-stream", ...opts.headers },
          credentials: "include",
          signal: controller.signal,
        });
        if (!res.ok || !res.body) throw new Error(`SSE ${res.status}`);
        opts.onState?.("live");
        attempt = 0;
        const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
        const parser = new SseParser();
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          for (const msg of parser.push(value)) {
            const ev = parseBoardEvent(msg.data);
            if (ev) opts.onEvent(ev);
          }
        }
      } catch {
        if (closed) return;
      }
      attempt += 1;
      const delay = Math.min(15000, 500 * 2 ** Math.min(attempt, 5));
      await new Promise((r) => setTimeout(r, delay));
    }
  };
  void run();
  return {
    close() {
      closed = true;
      controller?.abort();
    },
  };
}
