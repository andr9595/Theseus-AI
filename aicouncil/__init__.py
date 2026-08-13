"""Theseus AI - a local, subscription-backed multi-agent coding council.

Council members answer a task independently, critique each other's answers
anonymised, and a chairman weighs the critiques and applies the outcome.
Chat and Project modes share the same provider machinery under a simpler,
single-agent execution model.

Everything runs through locally installed, subscription-authenticated CLI
tools. No per-token API keys are read, stored or transmitted by this package.
"""

__version__ = "1.0.0"
APP_NAME = "Theseus AI"
