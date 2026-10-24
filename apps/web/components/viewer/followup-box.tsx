"use client";

import { Mail } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { useApi } from "@/components/api-provider";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import type { DocumentDetail, Followup } from "@/lib/api-types";

export function FollowupBox({ doc }: { doc: DocumentDetail }) {
  const { api } = useApi();
  const [draft, setDraft] = React.useState<Followup | null>(doc.followups[0] ?? null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  if (doc.status !== "NeedsReview" && doc.status !== "Flagged") return null;

  const create = async () => {
    if (!api) return;
    setBusy(true);
    try {
      setDraft(await api.createFollowup(doc.id));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      {draft ? (
        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
          <p className="text-sm text-slate-500">Draft to {draft.to_addr ?? "the vendor (address on file)"}</p>
          <p className="font-semibold">{draft.draft_subject}</p>
          <p className="mt-1 line-clamp-4 whitespace-pre-line text-slate-700">{draft.draft_body}</p>
          <Link href="/followups" className="mt-2 inline-block text-blue-700 underline">
            Review and send
          </Link>
        </div>
      ) : (
        <Button variant="outline" disabled={busy} onClick={() => void create()}>
          <Mail className="h-4 w-4" /> Draft an email to the vendor
        </Button>
      )}
      {error && <Alert tone="red">{error}</Alert>}
    </div>
  );
}
