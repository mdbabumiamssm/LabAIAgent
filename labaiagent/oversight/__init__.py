"""Agent oversight: watching the watchers.

The safety engine bounds WHAT an agent may do; this package watches HOW an
agent is behaving and intervenes when the pattern -- not any single call --
is the problem:

  - ``Supervisor``: streams every dispatch outcome, detects the signature
    agent failure (a burst of safety refusals: an agent arguing with the
    limits), and automatically SUSPENDS the offending identity. A suspended
    agent keeps read access and the e-stop, loses actuation, and stays
    suspended until a human reinstates it.
  - Reviewers -- a pre-execution second opinion on HIGH/CRITICAL actuation:
    ``RuleBasedReviewer`` (deterministic, zero dependencies, always on) or
    ``FoundationModelReviewer`` (an independent foundation model -- Claude or
    GPT -- asked to veto the call; opt-in, and FAIL-CLOSED: if the reviewer
    errors, the call is denied, never waved through).
  - ``FeedbackStore``: every human judgement about agent behaviour --
    approvals granted, actions rejected, cancellations, e-stops, suspensions,
    and explicit ratings via the submit_feedback tool -- is captured as a
    preference record and exportable as an RLHF/DPO-ready dataset. Training
    happens outside this process, on your infrastructure; this layer's job is
    to make the human-preference signal exist, attributably and honestly.
"""

from .feedback import FeedbackStore
from .supervisor import (
    FoundationModelReviewer,
    Reviewer,
    RuleBasedReviewer,
    Supervisor,
    Verdict,
)

__all__ = ["Supervisor", "Verdict", "Reviewer", "RuleBasedReviewer",
           "FoundationModelReviewer", "FeedbackStore"]
