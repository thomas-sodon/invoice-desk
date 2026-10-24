"use client";

import * as React from "react";
import type { ConnectionState } from "@/lib/api";
import type { BoardEvent, DocumentCard } from "@/lib/api-types";
import { columnFor } from "@/lib/columns";
import { addUsd } from "@/lib/money";
import { useApi } from "../api-provider";

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
    updated_at: e.ts,
  };
  const out = cards.slice();
  out[i] = next;
  return out;
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

  React.useEffect(() => {
    if (!api) return;
    const refreshCard = (id: string) =>
      api
        .getDocument(id)
        .then((d) =>
          setCards((prev) => {
            const card: DocumentCard = { ...d };
            const i = prev.findIndex((c) => c.id === id);
            if (i < 0) return [...prev, card];
            const out = prev.slice();
            out[i] = card;
            return out;
          }),
        )
        .catch(() => undefined);
    const sub = api.subscribe(
      batchId,
      (e) => {
        if (e.batch_cost_usd && (!batchId || e.batch_id === batchId)) setBatchCost(e.batch_cost_usd);
        if (e.type === "document.created" && e.document_id) void refreshCard(e.document_id);
        if (e.type === "document.status_changed" || e.type === "document.updated") {
          setCards((prev) => applyBoardEvent(prev, e));
          if (e.document_id) void refreshCard(e.document_id);
        }
      },
      setConn,
    );
    return () => sub.close();
  }, [api, batchId]);

  const shownCost = batchCost ?? addUsd(...cards.map((c) => c.cost_usd));
  return { cards, setCards, cost: shownCost, loading, error, reload: load, conn };
}
