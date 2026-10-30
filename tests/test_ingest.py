from app import rules as R
from app.ingest import EMPTY_STATS, ingest_candidate
from app.models import Exclusion, Lead, LeadHistory, PipelineEntry

from .conftest import GOOD


def run(db, *recs):
    st = dict(EMPTY_STATS)
    out = [ingest_candidate(db, r, None, st) for r in recs]
    return st, out


def test_verified_lead_scored_and_piped(db):
    st, (res,) = run(db, GOOD)
    assert res["outcome"] == "verified_lead_added" and st["new_verified"] == 1
    lead = db.query(Lead).one()
    # recent activity (+15) and LinkedIn (+10) are no longer researched, so this record scores 75
    assert lead.lead_id == "IN-US-000001" and lead.lead_score == 75 and lead.priority == "B"
    assert lead.us_state == "WY" and lead.entity_pair_key == "U72900KA2019PTC123456|WY:2020-000999999"
    assert db.query(PipelineEntry).count() == 1


def test_corporation_disqualified(db):
    st, (res,) = run(db, {**GOOD, "us_llc_legal_name": "Testco Inc."})
    assert res["outcome"] == "disqualified" and st["new_verified"] == 0
    assert db.query(Exclusion).one().type == "Disqualified"


def test_unofficial_registry_urls_cannot_verify(db):
    st, (res,) = run(db, {**GOOD, "us_registry_url": "https://www.opencorporates.example/x",
                          "us_verification_limitation": "Delaware ICIS CAPTCHA"})
    assert res["outcome"] == "queued_for_research" and st["partial"] == 1
    lead = db.query(Lead).one()
    assert lead.verification_status == "PARTIALLY VERIFIED" and "CAPTCHA" in lead.research_limitations
    st, (res,) = run(db, {**GOOD, "cin_llpin": "U72900KA2019PTC999999", "us_entity_number": "X1",
                          "indian_website": "https://other.example", "indian_legal_name": "Other Pvt Ltd",
                          "indian_registry_url": "https://www.zaubacorp.example/x"})
    assert st["new_verified"] == 0


def test_bad_cin_cannot_be_verified(db):
    st, _ = run(db, {**GOOD, "cin_llpin": "12345"})
    assert st["new_verified"] == 0


def test_rerun_updates_without_overwriting_manual_fields(db):
    run(db, GOOD)
    lead = db.query(Lead).one()
    lead.notes, lead.outreach_status = "Called CEO, interested", "Contacted"
    db.commit()
    st, (res,) = run(db, {**GOOD, "notes": "agent note", "outreach_status": "Not Contacted",
                          "business_phone": "+91-00000-00000"})
    assert res["outcome"] == "updated_existing" and st["new_verified"] == 0
    lead = db.query(Lead).one()
    assert lead.notes == "Called CEO, interested" and lead.outreach_status == "Contacted"
    assert lead.business_phone == "+91-00000-00000"
    assert db.query(LeadHistory).filter_by(field="business_phone").count() == 1


def test_queue_item_promoted_when_evidence_arrives(db):
    run(db, {**GOOD, "us_registry_url": None})
    st, (res,) = run(db, {**GOOD})
    assert res["outcome"] == "promoted_to_verified" and st["promoted"] == 1 and st["new_verified"] == 1
    lead = db.query(Lead).one()
    assert lead.lead_id == "IN-US-000001" and lead.verification_status == "VERIFIED"
    assert db.query(PipelineEntry).count() == 1


def test_partial_evidence_merges_across_submissions(db):
    """Evidence found on different days combines into one verified record."""
    run(db, {**GOOD, "relationship_evidence": None, "relationship_evidence_urls": []})
    st, (res,) = run(db, {k: GOOD[k] for k in ("cin_llpin", "us_state", "us_entity_number", "verification_status",
                                              "relationship_evidence", "relationship_evidence_urls")})
    assert res["outcome"] == "promoted_to_verified"


def test_soft_duplicate_flagged_not_added(db):
    other = {**GOOD, "cin_llpin": "U72900KA2019PTC654321", "us_entity_number": "2021-000111111"}
    st, (_, res) = run(db, GOOD, other)
    assert res["outcome"] == "possible_duplicate" and st["new_verified"] == 1 and st["duplicates"] == 1
    ex = db.query(Exclusion).one()
    assert ex.type == "Possible Duplicate - Review" and ex.canonical_lead_id == "IN-US-000001"


def test_verified_lead_never_demoted(db):
    run(db, GOOD)
    _, (res,) = run(db, {**GOOD, "us_registry_url": "https://www.unofficial-directory.example/testco"})
    lead = db.query(Lead).one()
    assert res["outcome"] == "kept_verified_flagged_for_review"
    assert lead.verification_status == "VERIFIED" and "review" in lead.research_limitations


def test_agent_cannot_upgrade_or_set_score(db):
    run(db, {**GOOD, "verification_status": "PARTIALLY VERIFIED", "lead_score": 100, "priority": "A"})
    lead = db.query(Lead).one()
    assert lead.verification_status == "PARTIALLY VERIFIED" and lead.lead_score < 100


def test_rules_helpers():
    assert R.valid_indian_id("U72900KA2019PTC123456") and R.valid_indian_id("AAB-1234")
    assert not R.valid_indian_id("U7290")
    assert R.is_official_us("https://search.sunbiz.org/x") and R.is_official_us("https://wyobiz.wyo.gov/x")
    assert not R.is_official_us("https://opencorporates.com/x")
    assert R.is_official_in("https://www.mca.gov.in/x") and not R.is_official_in("https://fakemca.gov.in.evil.example")
    assert R.norm_name("Acme Software Pvt. Ltd.") == R.norm_name("ACME SOFTWARE PRIVATE LIMITED")
    assert R.priority(80) == "A" and R.priority(79) == "B" and R.priority(59) == "C"
