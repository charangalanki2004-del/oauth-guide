import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MOCK_MODE", "1")

from app import agent, rubric  # noqa: E402
from app.schema import ApprovalGate, Claim, Evidence, Guide, RegistrationAccess, WaitRange, slugify  # noqa: E402
from app.search import Page  # noqa: E402


def _pages():
    return [
        Page(url="https://docs.example.com/oauth", title="OAuth docs", text="x" * 400, official=True),
        Page(url="https://blog.other.com/post", title="A blog", text="y" * 400, official=False),
    ]


def test_validate_drops_invented_urls_and_downgrades():
    raw = {
        "summary": "s",
        "approval_claim": {"text": "needs review", "evidence": "sourced", "source_url": "https://docs.example.com/made-up"},
        "redirect_uri_rules": {"text": "exact match", "evidence": "sourced", "source_url": "https://docs.example.com/oauth"},
        "token_lifecycle": {"text": "rotating refresh", "evidence": "sourced", "source_url": "https://blog.other.com/post"},
        "steps": [{"title": "a", "detail": "b", "actor": "human", "evidence": "sourced", "source_url": "https://nope.com"}],
    }
    g = agent.validate(raw, _pages(), {"name": "Example"}, "Example", [])
    assert g.approval_claim.source_url is None and g.approval_claim.evidence == Evidence.inferred
    assert g.redirect_uri_rules.source_url == "https://docs.example.com/oauth"
    assert g.redirect_uri_rules.evidence == Evidence.sourced
    # third-party source keeps the link but is not "sourced"
    assert g.token_lifecycle.source_url == "https://blog.other.com/post"
    assert g.token_lifecycle.evidence == Evidence.inferred
    assert g.steps[0].source_url is None and g.steps[0].evidence == Evidence.inferred
    assert len(g.sources) == 2


def test_validate_tolerates_bad_enum_and_missing_fields():
    raw = {"summary": "s", "approval_claim": {"text": "x", "evidence": "definitely", "source_url": None}}
    g = agent.validate(raw, [], {"name": "Thing"}, "Thing", [])
    assert g.approval_claim.evidence == Evidence.inferred
    assert g.approval_gate == ApprovalGate.unknown
    assert g.difficulty.level >= 1


def test_rubric_levels_are_ordered():
    easy = Guide(slug="a", name="a", registration_access=RegistrationAccess.anyone, approval_gate=ApprovalGate.none)
    hard = Guide(slug="b", name="b", registration_access=RegistrationAccess.partner_program,
                 approval_gate=ApprovalGate.review_required,
                 review_artifacts=[Claim(text="demo video"), Claim(text="security assessment (CASA)")],
                 approval_wait=WaitRange(max_days=45))
    e, h = rubric.score(easy), rubric.score(hard)
    assert e.level == 1 and e.label == "Trivial"
    assert h.level == 5 and h.label == "Very hard"
    assert any("security assessment" in r for r in h.reasons)
    assert any("over a month" in r for r in h.reasons)


def test_rubric_no_oauth_short_circuits():
    g = Guide(slug="c", name="c", oauth_supported=False, approval_gate=ApprovalGate.review_required)
    assert rubric.score(g).label == "No OAuth"


def test_token_trap_not_triggered_by_negation():
    g = Guide(slug="d", name="d", approval_gate=ApprovalGate.none,
              token_lifecycle=Claim(text="Tokens do not expire unless revoked."))
    assert "Token lifecycle has traps" not in rubric.score(g).reasons
    g2 = Guide(slug="e", name="e", approval_gate=ApprovalGate.none,
               token_lifecycle=Claim(text="Refresh tokens expire after 7 days in testing mode."))
    assert "Token lifecycle has traps" in rubric.score(g2).reasons


def test_slugify():
    assert slugify("https://www.strava.com/") == "strava"
    assert slugify("QuickBooks Online") == "quickbooks-online"
    assert slugify("linear.app") == "linear"


def test_confidence_needs_official_sources():
    g = Guide(slug="f", name="f", approval_claim=Claim(text="x", evidence=Evidence.sourced),
              redirect_uri_rules=Claim(text="y", evidence=Evidence.sourced))
    assert rubric.confidence(g) == "low"  # sourced claims but no official pages recorded


def test_user_content_hosts_are_not_official():
    from app.search import is_official
    roots = {"github.com"}
    assert is_official("https://docs.github.com/en/apps/oauth-apps", roots)
    assert is_official("https://github.com/settings/developers", roots)
    assert not is_official("https://github.com/learnthatstack/oauth-github-dashboard", roots)
    assert not is_official("https://someblog.com/github-oauth", roots)


def test_gists_and_pages_are_user_content():
    from app.search import is_official
    roots = {"github.com"}
    assert not is_official("https://gist.github.com/someone/24c8a6980e116e62f78", roots)
    assert not is_official("https://someone.github.io/post", roots)


def test_self_hosted_instance_note_does_not_score():
    g = Guide(slug="g", name="g", approval_gate=ApprovalGate.none,
              instance_specific=Claim(text="GitHub Enterprise Server instances may not support expiring tokens.",
                                      evidence=Evidence.sourced))
    assert "Tenant/instance-specific configuration" not in rubric.score(g).reasons
    g2 = Guide(slug="h", name="h", approval_gate=ApprovalGate.none,
               instance_specific=Claim(text="Each shop has its own myshopify.com domain used in the authorize URL.",
                                       evidence=Evidence.sourced))
    assert "Tenant/instance-specific configuration" in rubric.score(g2).reasons


def test_validate_drops_malformed_items_instead_of_failing():
    raw = {
        "summary": "s",
        "steps": [{"title": "ok", "detail": "d", "actor": "robot", "evidence": "sure"},   # bad enums -> normalised
                  {"title": "no detail"},                                                 # missing detail -> ""
                  {"detail": "no title"},                                                 # unusable -> dropped
                  "just a string"],
        "blockers": [{"blocker": "x"}, {"fix": "orphan"}],
        "approval_wait": {"min_days": "unknown", "max_days": "14", "evidence": "inferred", "note": "n"},
        "setup_time": {"min_minutes": 5.0, "max_minutes": None},
    }
    g = agent.validate(raw, [], {"name": "T"}, "T", [])
    assert [s.title for s in g.steps] == ["ok", "no detail"]
    assert g.steps[0].actor.value == "human" and g.steps[0].evidence == Evidence.inferred
    assert len(g.blockers) == 1 and g.blockers[0].fix == ""
    assert g.approval_wait.min_days is None and g.approval_wait.max_days == 14 and g.approval_wait.note == "n"
    assert g.setup_time.min_minutes == 5


def test_sources_block_respects_budget_official_first():
    pages = [Page(url="https://docs.x.com/a", title="a", text="A" * 10000, official=True),
             Page(url="https://blog.y.com/b", title="b", text="B" * 10000, official=False)]
    out = agent._sources_block(pages, budget=6000)
    # the official page takes the budget; the third-party page only gets the floor
    assert 5900 <= out.count("A") <= 6001 and out.count("B") <= 300
    assert "[...truncated]" in out
    # a short official page leaves the budget to the next page
    pages[0].text = "A" * 500
    out = agent._sources_block(pages, budget=6000)
    assert out.count("B") > 5000
    # two official pages split it evenly
    pages[0].text = "A" * 10000
    pages[1].official = True
    out = agent._sources_block(pages, budget=6000)
    assert 2900 <= out.count("A") <= 3100 and 2900 <= out.count("B") <= 3100


def test_too_large_error_is_detected_with_ratio():
    from app import llm
    e = llm._too_large(Exception("Error code: 413 - Request too large for model `x` on tokens per minute (TPM): "
                                 "Limit 8000, Requested 24000, please reduce your message size and try again."))
    assert e is not None and abs(e.ratio - 1 / 3) < 1e-6
    assert llm._too_large(Exception("Rate limit reached, retry in 2s")) is None


def test_repo_file_views_are_noise():
    from app.search import is_noise
    assert is_noise("https://github.com/github/docs/blob/main/content/apps/oauth-apps/x.md")
    assert is_noise("https://gitlab.com/gitlab-org/gitlab/-/blob/master/doc/api/oauth2.md")
    assert not is_noise("https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app")
    assert not is_noise("https://github.com/settings/developers")


def test_citations_match_canonically():
    pages = [Page(url="https://support.google.com/cloud/answer/9110914?hl=en", title="Consent", text="x" * 400, official=True)]
    raw = {"summary": "s", "approval_claim": {"text": "verify", "evidence": "sourced",
                                             "source_url": "https://support.google.com/cloud/answer/9110914"}}
    g = agent.validate(raw, pages, {"name": "G"}, "G", [])
    assert g.approval_claim.evidence == Evidence.sourced
    assert g.approval_claim.source_url == "https://support.google.com/cloud/answer/9110914?hl=en"
    # a different answer id is still an invented citation
    raw["approval_claim"]["source_url"] = "https://support.google.com/cloud/answer/9999999"
    g = agent.validate(raw, pages, {"name": "G"}, "G", [])
    assert g.approval_claim.evidence == Evidence.inferred and g.approval_claim.source_url is None
