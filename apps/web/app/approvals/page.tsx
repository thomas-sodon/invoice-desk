"use client";

import Link from "next/link";
import { Queue } from "@/components/queue";
import { Alert } from "@/components/ui/alert";
import { ApprovalPanel } from "@/components/viewer/approval-panel";
import { HiddenTextAlert } from "@/components/viewer/document-viewer";
import { PageOverlay } from "@/components/viewer/page-overlay";
import { RulesList } from "@/components/viewer/rules-list";
import { maskText } from "@/lib/mask";
import { hiddenTextSummary } from "@/lib/screening";

export default function ApprovalsPage() {
  return (
    <Queue
      title="Approvals"
      intro="Anything that touches money in a risky way needs a person. Bank changes need two approvers, a callback, and never the person who uploaded it."
      empty="No approvals waiting."
      load={async (api) => {
        const [flagged, review] = await Promise.all([
          api.listDocuments({ status: "Flagged" }),
          api.listDocuments({ status: "NeedsReview" }),
        ]);
        return [...flagged.items, ...review.items.filter((d) => d.badges.includes("new_vendor") || d.badges.includes("over_cap"))];
      }}
      renderDetail={(doc, done) => (
        <div className="grid gap-4 xl:grid-cols-2">
          <div>
            {doc.pages[0] && (
              <PageOverlay page={doc.pages[0]} fields={doc.fields} screen={doc.screen_results} selected={null} alt={doc.filename} redactions={doc.redactions} />
            )}
          </div>
          <div className="min-w-0 space-y-4">
            <h2 className="text-2xl font-bold">{doc.vendor_name ?? doc.filename}</h2>
            <HiddenTextAlert doc={doc} />
            {doc.headline && !hiddenTextSummary(doc.screen_results) && (
              <Alert tone="red" className="text-lg font-semibold">
                {maskText(doc.headline)}
              </Alert>
            )}
            <ApprovalPanel doc={doc} onDone={done} />
            <details>
              <summary className="cursor-pointer font-semibold">All checks</summary>
              <div className="mt-2">
                <RulesList rules={doc.rule_results} />
              </div>
            </details>
            <Link href={`/documents/${doc.id}`} className="inline-block text-blue-700 underline">
              Open full invoice
            </Link>
          </div>
        </div>
      )}
    />
  );
}
