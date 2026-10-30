"""Overseas entities outside the U.S. (UK, Australia, New Zealand, EU/EEA) and per-run targeting."""
from app import jurisdictions as J
from app import rules as R
from app.ingest import EMPTY_STATS, ingest_candidate
from app.models import Lead, Run
from app.research import planner, prompts

from .conftest import GOOD
from .test_web import client, csrf, login  # noqa: F401  (client is a fixture)

# SYNTHETIC: a UK subsidiary of the fake Testco, evidenced on Companies House.
UK = {**GOOD, "foreign_country": "UK", "us_llc_legal_name": "Testco Software Ltd", "us_state": None,
      "us_entity_number": "12345678",
      "us_registry_url": "https://find-and-update.company-information.service.gov.uk/company/12345678"}


def test_uk_entity_verified_on_companies_house(db):
    st = dict(EMPTY_STATS)
    res = ingest_candidate(db, UK, None, st)
    assert res["outcome"] == "verified_lead_added" and st["new_verified"] == 1
    lead = db.query(Lead).one()
    assert lead.foreign_country == "GB" and lead.entity_pair_key == "U72900KA2019PTC123456|@GB:12345678"
    assert lead.lead_id == "IN-GB-000001"


def test_non_official_or_wrong_form_cannot_verify():
    status, problems, _ = R.gate({**UK, "us_registry_url": "https://opencorporates.com/companies/gb/12345678"})
    assert status != "VERIFIED" and any("official company registry" in p for p in problems)
    status, problems, _ = R.gate({**UK, "us_llc_legal_name": "Testco Software"})
    assert status != "VERIFIED" and any("company-form designator" in p for p in problems)
    # A U.S. registry page is not evidence for a UK company, and vice versa.
    assert R.gate({**UK, "us_registry_url": GOOD["us_registry_url"]})[0] != "VERIFIED"


def test_eu_and_eea_registers():
    de = {**GOOD, "foreign_country": "Germany", "us_llc_legal_name": "Testco Software GmbH", "us_state": None,
          "us_registry_url": "https://www.handelsregister.de/rp_web/demo"}
    no = {**GOOD, "foreign_country": "NO", "us_llc_legal_name": "Testco Norge AS", "us_state": None,
          "us_registry_url": "https://virksomhet.brreg.no/nb/oppslag/enheter/912345678"}
    assert R.gate(de)[0] == "VERIFIED" and R.gate(no)[0] == "VERIFIED"
    status, problems, _ = R.gate({**de, "foreign_country": "SG"})
    assert status != "VERIFIED" and "Unsupported foreign jurisdiction: SG" in problems


def test_germany_never_collides_with_delaware():
    de_us = R.entity_pair_key({**GOOD, "us_state": "DE", "us_entity_number": "1"})
    de_eu = R.entity_pair_key({**GOOD, "foreign_country": "DE", "us_state": None, "us_entity_number": "1"})
    assert de_us != de_eu


def test_us_rules_unchanged_without_country():
    assert R.gate(GOOD)[0] == "VERIFIED"
    assert R.gate({**GOOD, "us_llc_legal_name": "Testco Inc."})[0] == "DISQUALIFIED"


def test_market_shortcuts_expand():
    assert J.expand_markets(["uk", "EU"])[:2] == ["GB", "AT"] and len(J.expand_markets(["EU"])) == 27
    assert set(J.expand_markets(["EEA"])) - set(J.EU) == {"IS", "LI", "NO"}


def test_targeted_plan_stays_in_focus(db):
    from app import appsettings
    s = appsettings.get_all(db)
    t = planner.clean_targets(markets=["UK", "bogus"], locations=["Chennai"], industries=["SaaS"], assignments=4)
    assert t == {"markets": ["GB"], "indian_locations": ["Chennai"], "industries": ["SaaS"], "assignments": 4}
    groups = planner.plan(db, s, t["assignments"], t)
    assert len(groups) == 4 and {g["market"] for g in groups} == {"GB"}
    assert all("Chennai" in q for g in groups for q in g["queries"])
    text = prompts.batch_prompt(groups[0], "2026-10-28", 10)
    assert "United Kingdom" in text and "Companies House" in text and "Only research companies" in text


def test_launch_form_queues_focused_run(client, db):
    login(client, "admin@example.com")
    assert client.get("/runs/new").status_code == 200
    tok = csrf(client, "/runs/new")
    bad = client.post("/runs", data={"csrf_token": tok, "custom": "1"}, follow_redirects=True)
    assert "Choose at least one country" in bad.text
    r = client.post("/runs", data={"csrf_token": tok, "custom": "1", "markets": ["GB", "AU"], "locations": "Mumbai",
                                   "locations_other": "Nashik", "assignments": "3", "revisit": "on"},
                    follow_redirects=True)
    assert r.status_code == 200 and "focus" in r.text
    run = db.query(Run).order_by(Run.id.desc()).first()
    assert run.targets == {"markets": ["GB", "AU"], "indian_locations": ["Mumbai", "Nashik"], "assignments": 3}
