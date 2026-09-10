"""Provider-independent governance gates for MMF-005."""

from .selection_gate import evaluate_selection
from .knowledge_usage_contract import build_contracts
from .commitment_provenance import evaluate_commitments, apply_local_repairs
from .artifact_qa import evaluate_artifact, apply_artifact_repairs, merge_longform_qa
from .final_artifact_qa import artifact_findings_are_locally_recoverable, evaluate_docx_artifact, quote_serialization_status, repair_docx_artifact
from .outbound_authorization import OutboundAuthorizationManifest

__all__ = [
    "evaluate_selection", "build_contracts", "evaluate_commitments",
    "apply_local_repairs", "evaluate_artifact", "apply_artifact_repairs", "merge_longform_qa",
    "OutboundAuthorizationManifest", "evaluate_docx_artifact", "repair_docx_artifact", "artifact_findings_are_locally_recoverable",
    "quote_serialization_status",
]
