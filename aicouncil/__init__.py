"""AI Council - a local, subscription-backed multi-agent coding pipeline.

Stage 1 (Junior Draft)  : the `codex` CLI drafts an implementation proposal.
Stage 2 (Senior Polish) : the `claude` CLI reviews, corrects and applies it.

Everything runs through locally installed, subscription-authenticated CLI
tools. No per-token API keys are read, stored or transmitted by this package.
"""

__version__ = "1.0.0"
APP_NAME = "AI Council"
