"use client";

import * as React from "react";
import type { ConnectionState } from "@/lib/api";
import type { BoardEvent, DocumentCard, DocumentDetail } from "@/lib/api-types";
import { columnFor } from "@/lib/columns";
import { addUsd } from "@/lib/money";
import { useApi } from "../api-provider";

/** The board keeps only card fields, not the whole detail payload. */
function toCard(d: DocumentDetail): DocumentCard {
  return {
    id: d.id,
    batch_id: d.batch_id,
    filename: d.filename,
    source: d.source,
    status: d.status,
    column: d.column,
    vendor_name: d.vendor_name,
    invoice_number: d.invoice_number,
    total_cents: d.total_cents,
    headline: d.headline,
    badges: d.badges,
    cost_usd: d.cost_usd,
    thumbnail_url: d.thumbnail_url,
    created_at: d.created_at,
    updated_at: d.updated_at,
  };
}

/** Pure reducer for realtime events, so card movement is testable without a DOM. */
export function applyBoardEvent(cards: DocumentCard[], e: BoardEvent): DocumentCard[] {
  if (!e.document_id) return cards;
  const i = cards.findIndex((c) => c.id === e.document_id);
  if (i < 0) return cards;
  const c = cards[i] as DocumentCard;
  const status = e.status ?? c.status;
  const next: DocumentCard = {
    ...c,
    status,
    column: e.column ?? columnFor(status),
    cost_usd: e.cost_usd ?? c.cost_usd,
    // updated_at stays the server's row timestamp so upsertCard can order re-fetches by it.
  };
  const out = cards.slice();
  out[i] = next;
  return out;
}

/**
 * Inserts or replaces a card from a fresh GET, unless the board already holds a newer version
 * (re-fetches can resolve out of order while a batch streams; an older response must not move a
 * card back to an earlier column).
 */
export function upsertCard(cards: DocumentCard[], card: DocumentCard): DocumentCard[] {
  const i = cards.findIndex((c) => c.id === card.id);
  if (i < 0) return [...cards, card];
  const cur = cards[i] as DocumentCard;
  if (Date.parse(cur.updated_at) > Date.parse(card.updated_at)) return cards;
  const out = cards.slice();
  out[i] = card;
  return out;
}

/** Only a batch view shows a batch's running cost; "all invoices" sums the visible cards. */
export function batchCostFromEvent(viewBatchId: string | null, e: BoardEvent): string | null {
  return viewBatchId && e.batch_id === viewBatchId && e.batch_cost_usd ? e.batch_cost_usd : null;
}

export function useBoard(batchId: string | null) {
  const { api } = useApi();
  const [cards, setCards] = React.useState<DocumentCard[]>([]);
  const [batchCost, setBatchCost] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const [conn, setConn] = React.useState<ConnectionState>("connecting");

  const load = React.useCallback(async () => {
    if (!api) return;
    try {
      if (batchId) {
        const b = await api.getBatch(batchId);
        setCards(b.documents);
        setBatchCost(b.cost_usd);
      } else {
        const list = await api.listDocuments({ limit: 200 });
        setCards(list.items);
        setBatchCost(null);
      }
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [api, batchId]);

  React.useEffect(() => {
    setLoading(true);
    void load();
  }, [load]);

  const loadRef = React.useRef(load);
  loadRef.current = load;

  React.useEffect(() => {
    if (!api) return;
    let wasDown = false;
    const refreshCard = (id: string) =>
      api
        .getDocument(id)
        .then((d) => setCards((prev) => upsertCard(prev, toCard(d))))
        .catch(() => undefined);
    const sub = api.subscribe(
      batchId,
      (e) => {
        const cost = batchCostFromEvent(batchId, e);
        if (cost) setBatchCost(cost);
        if (e.type === "document.created" && e.document_id) void refreshCard(e.document_id);
        if (e.type === "document.status_changed" || e.type === "document.updated") {
          setCards((prev) => applyBoardEvent(prev, e));
          if (e.document_id) void refreshCard(e.document_id);
        }
      },
      (state) => {
        setConn(state);
        // Events sent while the stream was down are lost: re-read the board after a reconnect.
        if (state === "reconnecting") wasDown = true;
        else if (state === "live" && wasDown) {
          wasDown = false;
          void loadRef.current();
        }
      },
    );
    return () => sub.close();
  }, [api, batchId]);

  const shownCost = batchCost ?? addUsd(...cards.map((c) => c.cost_usd));
  return { cards, setCards, cost: shownCost, loading, error, reload: load, conn };
}
