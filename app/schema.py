"""
The Guide schema.

A guide is a structured object, not prose. Every load-bearing claim carries an
evidence tier so a reader (and the rubric) can tell what was actually tested,
what an official source says, and what is an inference.

Evidence tiers:
  tested   - a human or the agent actually attempted it and observed the result
  sourced  - an official provider page (or equally authoritative source) says so
  inferred - community reports, extrapolation, or the model's best guess
  unknown  - we looked and could not find out
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class Evidence(str, Enum):
    tested = "tested"
    sourced = "sourced"
    inferred = "inferred"
    unknown = "unknown"


class Actor(str, Enum):
    agent = "agent"      # an agent can do this end to end
    human = "human"      # requires login, identity, payment, legal, or judgment
    either = "either"    # agent can draft, human confirms


class RegistrationAccess(str, Enum):
    anyone = "anyone"                  # any account can create an app
    workspace_admin = "workspace_admin"  # needs org/workspace admin rights
    paid_tier = "paid_tier"            # needs a paid developer plan
    partner_program = "partner_program"  # needs partner/business program enrolment
    unknown = "unknown"


class ApprovalGate(str, Enum):
    none = "none"                        # no review for private/unlisted use
    soft_cap = "soft_cap"                # works immediately, user/usage cap until reviewed
    marketplace_only = "marketplace_only"  # review only if you want a public listing
    review_for_scopes = "review_for_scopes"  # certain scopes need review before real use
    review_required = "review_required"  # any production use needs review
    partner_only = "partner_only"        # OAuth only available to approved partners
    unknown = "unknown"


class Claim(BaseModel):
    """A single fact with provenance."""
    text: str
    evidence: Evidence = Evidence.inferred
    source_url: Optional[str] = None


class TimeRange(BaseModel):
    min_minutes: Optional[int] = None
    max_minutes: Optional[int] = None
    evidence: Evidence = Evidence.inferred
    source_url: Optional[str] = None
    note: Optional[str] = None


class WaitRange(BaseModel):
    min_days: Optional[int] = None
    max_days: Optional[int] = None
    evidence: Evidence = Evidence.inferred
    source_url: Optional[str] = None
    note: Optional[str] = None


class AppType(BaseModel):
    name: str
    when_to_use: str
    recommended_for_agents: bool = False


class Step(BaseModel):
    title: str
    detail: str
    actor: Actor = Actor.human
    evidence: Evidence = Evidence.inferred
    source_url: Optional[str] = None


class Blocker(BaseModel):
    blocker: str
    fix: str
    evidence: Evidence = Evidence.inferred
    source_url: Optional[str] = None


class Source(BaseModel):
    url: str
    title: str = ""
    fetched_at: Optional[str] = None
    official: bool = False


class AgentAttempt(BaseModel):
    """Something the investigating agent actually tried against the provider."""
    action: str
    result: str
    ok: Optional[bool] = None


class Difficulty(BaseModel):
    score: float = 0.0           # raw rubric points
    level: int = 1               # 1..5
    label: str = "Unknown"
    reasons: List[str] = Field(default_factory=list)


class Guide(BaseModel):
    # identity
    slug: str
    name: str
    website: Optional[str] = None
    category: Optional[str] = None
    developer_portal_url: Optional[str] = None
    summary: str = ""            # two sentences: the verdict

    # does this app even do OAuth?
    oauth_supported: Optional[bool] = None
    auth_methods: List[str] = Field(default_factory=list)  # e.g. ["oauth2", "api_key"]
    oauth_note: Optional[Claim] = None

    # which kind of app to create
    app_types: List[AppType] = Field(default_factory=list)

    # registration
    registration_access: RegistrationAccess = RegistrationAccess.unknown
    registration_access_claim: Optional[Claim] = None
    prerequisites: List[Claim] = Field(default_factory=list)
    steps: List[Step] = Field(default_factory=list)

    # approval
    approval_gate: ApprovalGate = ApprovalGate.unknown
    approval_claim: Optional[Claim] = None
    unreviewed_limits: Optional[Claim] = None      # what you get before approval
    review_artifacts: List[Claim] = Field(default_factory=list)  # demo video, privacy policy...
    approval_wait: WaitRange = Field(default_factory=WaitRange)

    # effort
    setup_time: TimeRange = Field(default_factory=TimeRange)

    # nuances
    redirect_uri_rules: Optional[Claim] = None
    token_lifecycle: Optional[Claim] = None
    scopes_note: Optional[Claim] = None
    instance_specific: Optional[Claim] = None    # tenant/subdomain/region quirks
    pkce_required: Optional[Claim] = None

    # trouble
    blockers: List[Blocker] = Field(default_factory=list)

    # handoff
    agent_can: List[str] = Field(default_factory=list)
    human_must: List[str] = Field(default_factory=list)

    # composio context
    composio_managed: Optional[bool] = None
    composio_note: Optional[str] = None

    # evidence bookkeeping
    sources: List[Source] = Field(default_factory=list)
    agent_attempts: List[AgentAttempt] = Field(default_factory=list)
    unverified_claims: List[str] = Field(default_factory=list)
    tested_notes: List[Claim] = Field(default_factory=list)  # merged from data/tested/*.json
    confidence: str = "low"      # low | medium | high  (computed)

    # computed
    difficulty: Difficulty = Field(default_factory=Difficulty)

    # meta
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    generator: str = ""          # model + provider used
    input_query: str = ""        # what the user typed


def slugify(name: str) -> str:
    import re
    s = name.strip().lower()
    s = re.sub(r"^https?://(www\.)?", "", s)
    s = re.sub(r"\.(com|io|dev|app|ai|co|org|net)(/.*)?$", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "app"
