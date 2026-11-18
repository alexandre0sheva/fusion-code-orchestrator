"""The experience library: per-project lessons learned from the host's feedback on runs.

``store`` keeps lessons, feedback and lesson use in Fusion's own database (never inside the
project); ``distil`` turns one run's feedback into at most one lesson; ``retrieve`` picks the
lessons that fit a new request and renders them, bounded, for the panel prompt. Off unless
``library.enabled`` is true.
"""
