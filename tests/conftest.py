import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_tmp = tempfile.mkdtemp()
os.environ.setdefault("DATA_DIR", _tmp)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/test.db")

from app import db as dbmod  # noqa: E402

# SYNTHETIC test record - every name and domain is fake (example.* / DEMO).
GOOD = dict(
    verification_status="VERIFIED",
    indian_legal_name="Testco Software Private Limited", indian_brand_name="Testco",
    cin_llpin="U72900KA2019PTC123456", indian_registered_state="Karnataka",
    indian_registry_url="https://www.mca.gov.in/mcafoportal/companyLLPMasterData.do#test",
    indian_website="https://www.testco.example",
    us_llc_legal_name="Testco Software LLC", us_state="wy", us_entity_number="2020-000999999",
    us_registry_url="https://wyobiz.wyo.gov/Business/FilingDetails.aspx?eFNum=TEST",
    relationship_evidence="Website Terms name both Testco Software Pvt Ltd and Testco Software LLC",
    relationship_evidence_urls=["https://www.testco.example/terms"],
    founder_ceo_name="Test Founder", linkedin_decision_maker_url="https://linkedin.com/in/test",
    recent_activity_evidence="Blog post Oct 2026",
    bookkeeping_need_signals="SaaS subscription revenue from US customers",
    outreach_angle="US LLC bills US SaaS customers; India ops team",
    source_urls=["https://www.testco.example/about"],
)


@pytest.fixture()
def db(tmp_path):
    # TEST_DATABASE_URL=postgresql+psycopg://... runs the suite against Postgres instead of SQLite.
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/t.db"
    eng = dbmod.configure(url)
    from app import models  # noqa: F401
    dbmod.Base.metadata.drop_all(eng)
    dbmod.init_db()
    s = dbmod.SessionLocal()
    yield s
    s.close()
    eng.dispose()
