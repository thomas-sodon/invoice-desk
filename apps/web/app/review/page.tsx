"use client";

import Link from "next/link";
import * as React from "react";
import { Queue } from "@/components/queue";
import { Alert } from "@/components/ui/alert";
import { CorrectionForm } from "@/components/viewer/correction-form";
import { FollowupBox } from "@/components/viewer/followup-box";
import { PageOverlay } from "@/components/viewer/page-overlay";
import { RulesList } from "@/components/viewer/rules-list";
import { maskText } from "@/lib/mask";

export default function ReviewPage() {
  const [selected, setSelected] = React.useState<string | null>(null);
  return (
    <Queue
      title="Needs review"
      intro="Each invoice shows the problem field highlighted and the reason in one sentence. Fix it and the checks run again."
      empty="Nothing to review. Every invoice is either ready to pay or waiting on an approval."
      load={async (api) => (await api.listDocuments({ status: "NeedsReview" })).items}
      renderDetail={(doc, done) => (
        <div className="grid gap-4 xl:grid-cols-2">
          <div>
            {doc.pages[0] && (
              <PageOverlay page={doc.pages[0]} fields={doc.fields} screen={doc.screen_results} selected={selected} onSelect={setSelected} alt={doc.filename} redactions={doc.redactions} />
            )}
          </div>
          <div className="min-w-0 space-y-4">
            <h2 className="text-2xl font-bold">{doc.vendor_name ?? doc.filename}</h2>
            {doc.headline && <Alert tone="amber" className="text-lg font-semibold">{maskText(doc.headline)}</Alert>}
            <CorrectionForm doc={doc} onDone={done} />
            <details>
              <summary className="cursor-pointer font-semibold">All checks</summary>
              <div className="mt-2">
                <RulesList rules={doc.rule_results} />
              </div>
            </details>
            <FollowupBox doc={doc} />
            <Link href={`/documents/${doc.id}`} className="inline-block text-blue-700 underline">
              Open full invoice
            </Link>
          </div>
        </div>
      )}
    />
  );
}
