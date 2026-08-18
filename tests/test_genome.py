"""
Tests for the two pieces most likely to be wrong in a real deployment:
identity resolution and blast-radius attenuation.

Run:  python -m pytest tests/ -v      (or: python tests/test_genome.py)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from common.identity import IdentityResolver, normalise, name_from_arn
from common.models import Service, Tier, Source, Dependency, DependencyKind
from queries.blast_radius import (Graph, blast_radius, critical_path_score,
                                  single_points_of_failure)


# ----------------------------------------------------------------- identity
def test_normalise_strips_env_and_platform_noise():
    assert normalise("prod-payments-api-svc") == "payments"
    assert normalise("payments_api") == "payments"
    assert normalise("PAYMENTS-API-PROD") == "payments"
    assert normalise("staging-checkout-service") == "checkout"


def test_name_from_arn_extracts_logical_name():
    assert name_from_arn(
        "arn:aws:ecs:us-east-1:123456789012:service/prod-cluster/payments-api"
    ) == "payments-api"
    assert name_from_arn(
        "arn:aws:lambda:us-east-1:123456789012:function:checkout-fn"
    ) == "checkout-fn"
    assert name_from_arn("not-an-arn") is None


def test_explicit_tag_beats_everything():
    r = IdentityResolver()
    res = r.resolve(
        name="totally-different-name", environment="prod",
        arn="arn:aws:ecs:us-east-1:1:service/c/whatever",
        tags={"cre:service-id": "payments-api"},
    )
    assert res.method == "explicit"
    assert res.service_id == "prod/payments-api"
    assert res.confidence == 1.0


def test_five_sources_converge_on_one_service():
    """The core requirement: same service, five names, one node."""
    r = IdentityResolver()
    ids = {
        r.resolve(name="payments-api", environment="prod",
                  arn="arn:aws:ecs:us-east-1:1:service/prod-cluster/payments-api-svc").service_id,
        r.resolve(name="payments_api", environment="prod").service_id,
        r.resolve(name="prod-payments-api", environment="prod").service_id,
        r.resolve(name="PaymentsAPI", environment="prod").service_id,
    }
    assert len(ids) == 1, f"identity fragmented across {ids}"


def test_ambiguous_match_goes_to_review_not_merged():
    """A wrong merge is worse than a missing one — it invents dependencies."""
    r = IdentityResolver()
    r.resolve(name="payments-api", environment="prod")
    res = r.resolve(name="payment-gateway", environment="prod")
    assert res.method in ("new", "structural")     # NOT silently merged
    # similar-but-not-equal names should surface for a human
    assert all(x.needs_review for x in r.review_queue)


def test_merge_never_downgrades_tier_or_blanks_fields():
    existing = Service(id="prod/pay", name="pay", environment="prod",
                       account="1", region="us-east-1", tier=Tier.TIER0,
                       owner_team="payments", sources=["aws-config"])
    incoming = Service(id="prod/pay", name="pay", environment="prod",
                       account="1", region="us-east-1", tier=Tier.TIER3,
                       owner_team=None, revenue_per_minute=4200.0,
                       sources=["xray"])
    r = IdentityResolver()
    merged = r.merge_sources(existing, incoming)
    assert merged.tier == Tier.TIER0            # most severe assessment wins
    assert merged.owner_team == "payments"      # not blanked by the None
    assert merged.revenue_per_minute == 4200.0  # new information accepted
    assert merged.sources == ["aws-config", "xray"]
    assert merged.is_multi_sourced              # completeness gate signal


# ------------------------------------------------------------- dependencies
def test_source_confidence_separates_observed_from_inferred():
    observed = Dependency("a", "b", DependencyKind.SYNC, Source.XRAY)
    inferred = Dependency("a", "b", DependencyKind.SYNC, Source.CONFIG)
    assert observed.confidence > inferred.confidence
    assert observed.propagation_score > inferred.propagation_score


def test_self_dependency_rejected():
    try:
        Dependency("a", "a", DependencyKind.SYNC, Source.XRAY)
        assert False, "should have raised"
    except ValueError:
        pass


def test_async_attenuates_more_than_sync():
    s = Dependency("a", "b", DependencyKind.SYNC, Source.XRAY).propagation_score
    a = Dependency("a", "b", DependencyKind.ASYNC, Source.XRAY).propagation_score
    assert s > a


# ---------------------------------------------------------- blast radius
def _estate() -> Graph:
    g = Graph()
    g.add_service("prod/db", tier="tier1", revenue_per_minute=0)
    g.add_service("prod/payments-api", tier="tier0", revenue_per_minute=4200)
    g.add_service("prod/checkout", tier="tier0", revenue_per_minute=9100)
    g.add_service("prod/reporting", tier="tier3", revenue_per_minute=10)
    g.add_service("prod/email", tier="tier2", revenue_per_minute=0)
    # payments and reporting both need db; checkout needs payments (sync)
    g.add_dependency("prod/payments-api", "prod/db", 0.95, "sync")
    g.add_dependency("prod/reporting", "prod/db", 0.57, "data")
    g.add_dependency("prod/checkout", "prod/payments-api", 0.95, "sync")
    g.add_dependency("prod/email", "prod/payments-api", 0.38, "async")
    return g


def test_blast_radius_attenuates_with_depth():
    g = _estate()
    hits = {i.service_id: i for i in blast_radius(g, "prod/db")}
    assert "prod/payments-api" in hits
    assert "prod/checkout" in hits
    # checkout is one hop further than payments, so strictly less impacted
    assert hits["prod/checkout"].impact < hits["prod/payments-api"].impact


def test_weak_async_path_falls_below_floor():
    g = _estate()
    hits = {i.service_id for i in blast_radius(g, "prod/db")}
    # db -> payments (0.95) -> email (0.38) = 0.36 ... above floor
    assert "prod/email" in hits
    # but with a stricter floor it drops out
    strict = {i.service_id for i in blast_radius(g, "prod/db", impact_floor=0.5)}
    assert "prod/email" not in strict


def test_cycle_terminates():
    g = Graph()
    for n in ("a", "b", "c"):
        g.add_service(f"prod/{n}", tier="tier2", revenue_per_minute=1)
    g.add_dependency("prod/a", "prod/b", 0.9)
    g.add_dependency("prod/b", "prod/c", 0.9)
    g.add_dependency("prod/c", "prod/a", 0.9)      # cycle
    result = blast_radius(g, "prod/a")             # must not hang
    assert len(result) == 2


def test_stale_edges_excluded_by_default():
    g = _estate()
    g.add_dependency("prod/legacy", "prod/db", 0.9, "sync", stale=True)
    g.add_service("prod/legacy", tier="tier2", revenue_per_minute=5)
    assert "prod/legacy" not in {i.service_id for i in blast_radius(g, "prod/db")}
    assert "prod/legacy" in {i.service_id
                             for i in blast_radius(g, "prod/db", include_stale=True)}


def test_critical_path_ranks_by_revenue_not_fanout():
    g = _estate()
    db = critical_path_score(g, "prod/db")
    email = critical_path_score(g, "prod/email")
    assert db > email, "shared datastore must outrank a leaf notifier"


def test_spof_identifies_tier0_exposure():
    g = _estate()
    spofs = {s["service_id"] for s in single_points_of_failure(g)}
    assert "prod/db" in spofs
    assert "prod/payments-api" in spofs
    assert "prod/reporting" not in spofs      # nothing tier-0 depends on it


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); passed += 1; print(f"  PASS  {name}")
            except Exception as e:
                failed += 1; print(f"  FAIL  {name}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
