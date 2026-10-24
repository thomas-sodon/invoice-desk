import { Suspense } from "react";
import { Board } from "@/components/board/board";

export default function BoardPage() {
  return (
    <Suspense fallback={<p className="text-slate-600">Loading board…</p>}>
      <Board />
    </Suspense>
  );
}
