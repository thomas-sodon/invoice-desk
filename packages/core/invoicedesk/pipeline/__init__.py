"""LangGraph document pipeline (M9, M10, M13, M20).

* :mod:`.graph`: the ``StateGraph`` (preprocess -> screen -> extract_and_score -> validate ->
  review_interrupt / post), the ``PostgresSaver`` checkpointer and :class:`PipelineRunner`.
* :mod:`.services`: the API's service layer (corrections, approvals, reject, posting,
  comparison).
* :mod:`.naive`: config N, the dry-run "Basic AI" baseline for comparison mode.
* :mod:`.events`: board events and :class:`PgNotifyPublisher`.
"""

from __future__ import annotations

from invoicedesk.pipeline.deps import PipelineDeps, build_deps
from invoicedesk.pipeline.events import PgNotifyPublisher
from invoicedesk.pipeline.graph import PipelineRunner, build_graph, postgres_checkpointer
from invoicedesk.pipeline.posting import PostingError, PostResult
from invoicedesk.pipeline.services import (
    ApprovalResult,
    CorrectionResult,
    DuplicateVote,
    PipelineServices,
    SelfApproval,
)

__all__ = [
    "ApprovalResult",
    "CorrectionResult",
    "DuplicateVote",
    "PgNotifyPublisher",
    "PipelineDeps",
    "PipelineRunner",
    "PipelineServices",
    "PostResult",
    "PostingError",
    "SelfApproval",
    "build_deps",
    "build_graph",
    "postgres_checkpointer",
]
