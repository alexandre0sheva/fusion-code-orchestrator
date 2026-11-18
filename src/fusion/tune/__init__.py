"""`fusion tune`: Propose–Probe–Compose over Fusion's prompt pack (after Harness²)."""

from fusion.tune.compose import contrast, merge_edits
from fusion.tune.probe import Effect, Probe, compare, split_holdout
from fusion.tune.propose import Edit, ModelTuner, Tuner, TunerReply, validate_edits
from fusion.tune.run import EvidenceRow, TuneConfig, TuneResult, render_evidence, run_tune

__all__ = [
    "Edit",
    "Effect",
    "EvidenceRow",
    "ModelTuner",
    "Probe",
    "TuneConfig",
    "TuneResult",
    "Tuner",
    "TunerReply",
    "compare",
    "contrast",
    "merge_edits",
    "render_evidence",
    "run_tune",
    "split_holdout",
    "validate_edits",
]
