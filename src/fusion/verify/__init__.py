"""Verified generation: panel-written checks, validated, then run against every candidate patch.

``synthesis`` is what members write (patch, checks, guardrails) and the evidence boundary;
``validate`` rejects a check before it runs; ``crosscheck`` runs every patch against every check
in sandbox copies and picks one. Only ``crosscheck`` runs code, and only through
``fusion.bench.sandbox``: import it explicitly, so importing this package runs nothing.
"""

from fusion.verify.synthesis import Candidate, CheckScript
from fusion.verify.validate import validate_check

__all__ = ["Candidate", "CheckScript", "validate_check"]
