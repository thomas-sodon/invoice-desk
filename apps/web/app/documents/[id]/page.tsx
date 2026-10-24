"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { Alert } from "@/components/ui/alert";
import { useDocument } from "@/components/use-document";
import { DocumentViewer } from "@/components/viewer/document-viewer";
import { errorMessage } from "@/lib/api";

export default function DocumentPage() {
  const params = useParams<{ id: string }>();
  const { doc, error, reload } = useDocument(params.id);
  return (
    <div className="space-y-4">
      <Link href="/" className="inline-flex items-center gap-1 text-blue-700 underline">
        <ArrowLeft className="h-4 w-4" /> Back to board
      </Link>
      {error ? <Alert tone="red">{errorMessage(error)}</Alert> : doc ? <DocumentViewer doc={doc} onChanged={reload} /> : <p>Loading…</p>}
    </div>
  );
}
