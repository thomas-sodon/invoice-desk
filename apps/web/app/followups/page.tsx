"use client";

import Link from "next/link";
import * as React from "react";
import { useApi, useApiData } from "@/components/api-provider";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { errorMessage } from "@/lib/api";
import type { Followup } from "@/lib/api-types";
import { maskText } from "@/lib/mask";
import { shortDate } from "@/lib/utils";

function Draft({ f, onDone }: { f: Followup; onDone: () => void }) {
  const { api } = useApi();
  const [subject, setSubject] = React.useState(f.draft_subject);
  const [body, setBody] = React.useState(f.draft_body);
  const [editing, setEditing] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const sent = !!f.approved_by;

  const approve = async () => {
    if (!api) return;
    setBusy(true);
    setError(null);
    try {
      const edits = editing ? { subject, body } : {};
      await api.approveFollowup(f.id, edits);
      onDone();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <p className="text-sm text-slate-500">To {f.to_addr ?? "vendor address on file"}</p>
        {editing ? (
          <input aria-label="Subject" className="h-11 rounded-md border border-slate-300 px-3 text-lg font-semibold" value={subject} onChange={(e) => setSubject(e.target.value)} />
        ) : (
          <CardTitle>{f.draft_subject}</CardTitle>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {editing ? (
          <textarea aria-label="Body" rows={9} className="w-full rounded-md border border-slate-300 p-3" value={body} onChange={(e) => setBody(e.target.value)} />
        ) : (
          <p className="whitespace-pre-line text-slate-800">{maskText(f.draft_body)}</p>
        )}
        {sent ? (
          <Alert tone="green">Approved and sent {f.sent_at ? shortDate(f.sent_at) : "(sending…)"}.</Alert>
        ) : (
          <div className="flex flex-wrap gap-2">
            <Button variant="success" disabled={busy} onClick={() => void approve()}>
              {editing ? "Save and send" : "Approve and send"}
            </Button>
            {!editing && (
              <Button variant="outline" onClick={() => setEditing(true)}>
                Edit
              </Button>
            )}
          </div>
        )}
        <Link href={`/documents/${f.document_id}`} className="inline-block text-blue-700 underline">
          View the invoice
        </Link>
        {error && <Alert tone="red">{error}</Alert>}
      </CardContent>
    </Card>
  );
}

export default function FollowupsPage() {
  const { data, error, reload } = useApiData((api) => api.listFollowups());
  const items = data?.items ?? [];
  const drafts = items.filter((f) => !f.approved_by);
  const done = items.filter((f) => f.approved_by);
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-3xl font-extrabold tracking-tight">Vendor emails</h1>
        <p className="text-lg text-slate-600">Drafted for you, sent only after a person approves.</p>
      </div>
      {error ? <Alert tone="red">{errorMessage(error)}</Alert> : null}
      {data && drafts.length === 0 && <Alert tone="green">No drafts waiting. Draft one from any invoice that needs review.</Alert>}
      <div className="grid gap-4 lg:grid-cols-2">
        {drafts.map((f) => (
          <Draft key={f.id} f={f} onDone={reload} />
        ))}
      </div>
      {done.length > 0 && (
        <>
          <h2 className="pt-4 text-xl font-bold">Sent</h2>
          <div className="grid gap-4 lg:grid-cols-2">
            {done.map((f) => (
              <Draft key={f.id} f={f} onDone={reload} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
