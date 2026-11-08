"""Code understanding for the Developer agent: snapshots, analysers, findings, autofixes, index."""

from hapm_codeintel.analyzers import RULES, analyze_file, analyze_snapshot
from hapm_codeintel.findings import Finding, dedupe, fingerprint, verify_location
from hapm_codeintel.fixes import FixError, apply_fix, unified_diff
from hapm_codeintel.snapshot import Snapshot, is_excluded, looks_secret

__all__ = [
    "RULES",
    "Finding",
    "FixError",
    "Snapshot",
    "analyze_file",
    "analyze_snapshot",
    "apply_fix",
    "dedupe",
    "fingerprint",
    "is_excluded",
    "looks_secret",
    "unified_diff",
    "verify_location",
]
