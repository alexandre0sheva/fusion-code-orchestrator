"""End-to-end Claude Code A/B: headless ``claude -p`` on executable tasks, with and without Fusion.

``runner`` runs the study (each run in a sandboxed temp copy of the task's repo), ``claude_cli``
builds the command and reads what Claude Code prints, ``metrics`` turns a run into the bench's
measurements, and ``fake_claude`` stands in for ``claude`` in ``--mock`` runs and tests.
"""
