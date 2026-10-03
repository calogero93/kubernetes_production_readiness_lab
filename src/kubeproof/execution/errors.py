"""Execution errors shared without importing optional AI frameworks."""


class TrialInfrastructureError(RuntimeError):
    """Infrastructure prevented a complete trial measurement."""
