"""Lead field catalogue: one place that maps DB attribute <-> workbook column label."""

# (attribute, label, group)
LEAD_FIELDS = [
    ("lead_id", "Lead ID", "id"),
    ("entity_pair_key", "Entity Pair Key", "id"),
    ("verification_status", "Verification Status", "score"),
    ("confidence_score", "Confidence Score", "score"),
    ("lead_score", "Lead Score", "score"),
    ("priority", "Priority", "score"),
    ("indian_legal_name", "Indian Legal Name", "india"),
    ("indian_brand_name", "Indian Brand Name", "india"),
    ("cin_llpin", "CIN/LLPIN", "india"),
    ("indian_registered_state", "Indian Registered State", "india"),
    ("indian_company_status", "Indian Company Status", "india"),
    ("indian_incorporation_date", "Indian Incorporation Date", "india"),
    ("indian_registry_url", "Indian Registry URL", "india"),
    ("indian_website", "Indian Website", "india"),
    ("foreign_country", "Foreign Country", "us"),
    ("us_llc_legal_name", "Foreign Entity Legal Name", "us"),
    ("us_state", "US State", "us"),
    ("us_entity_number", "Foreign Entity Number", "us"),
    ("us_llc_status", "Foreign Entity Status", "us"),
    ("us_formation_date", "Foreign Formation Date", "us"),
    ("us_registry_url", "Foreign Registry URL", "us"),
    ("us_verification_limitation", "Foreign Verification Limitation", "us"),
    ("relationship_type", "Relationship Type", "relationship"),
    ("relationship_evidence", "Relationship Evidence", "relationship"),
    ("relationship_evidence_urls", "Relationship Evidence URLs", "relationship"),
    ("industry", "Industry", "company"),
    ("business_category", "Business Category", "company"),
    ("company_description", "Company Description", "company"),
    ("employee_range", "Employee Range", "company"),
    ("founder_ceo_name", "Founder/CEO Name", "contact"),
    ("decision_maker_name", "Decision-Maker Name", "contact"),
    ("decision_maker_title", "Decision-Maker Title", "contact"),
    ("business_email", "Business Email", "contact"),
    ("email_source_url", "Email Source URL", "contact"),
    ("email_verification_status", "Email Verification Status", "contact"),
    ("email_verification_date", "Email Verification Date", "contact"),
    ("linkedin_company_url", "LinkedIn Company URL", "contact"),
    ("linkedin_decision_maker_url", "LinkedIn Decision-Maker URL", "contact"),
    ("business_phone", "Business Phone", "contact"),
    ("indian_address", "Indian Address", "contact"),
    ("us_address", "Foreign Address", "contact"),
    ("recent_activity_evidence", "Recent Activity Evidence", "sales"),
    ("bookkeeping_need_signals", "Bookkeeping Need Signals", "sales"),
    ("outreach_angle", "Outreach Angle", "sales"),
    ("next_sales_action", "Next Sales Action", "sales"),
    ("source_urls", "Source URLs", "relationship"),
    ("date_first_discovered", "Date First Discovered", "meta"),
    ("date_last_checked", "Date Last Checked", "meta"),
    ("last_enrichment_date", "Last Enrichment Date", "meta"),
    ("outreach_status", "Outreach Status", "sales"),
    ("assigned_owner", "Assigned Owner", "sales"),
    ("notes", "Notes", "sales"),
    ("research_limitations", "Research Limitations", "meta"),
]

# Essential columns exported to Excel (removes internal DB/research junk columns)
EXCEL_FIELDS = [
    ("indian_legal_name", "Indian Legal Name", "india"),
    ("indian_brand_name", "Indian Brand Name", "india"),
    ("indian_website", "Indian Website", "india"),
    ("foreign_country", "Foreign Country", "us"),
    ("us_llc_legal_name", "Foreign Entity Legal Name", "us"),
    ("us_state", "US State", "us"),
    ("industry", "Industry", "company"),
    ("business_category", "Business Category", "company"),
    ("founder_ceo_name", "Founder/CEO Name", "contact"),
    ("decision_maker_name", "Decision-Maker Name", "contact"),
    ("decision_maker_title", "Decision-Maker Title", "contact"),
    ("business_email", "Business Email", "contact"),
    ("email_source_url", "Email Source URL", "contact"),
    ("business_phone", "Business Phone", "contact"),
    ("bookkeeping_need_signals", "Bookkeeping Need Signals", "sales"),
    ("outreach_angle", "Outreach Angle", "sales"),
    ("source_urls", "Source URLs", "relationship"),
    ("outreach_status", "Outreach Status", "sales"),
]
QUEUE_EXTRA = [
    ("missing_evidence", "Missing Evidence", "meta"),
    ("next_research_step", "Next Research Step", "meta"),
    ("times_revisited", "Times Revisited", "meta"),
]

LABEL = {a: l for a, l, _ in LEAD_FIELDS + QUEUE_EXTRA}

# Fields the research agent may write. Everything else is set by the engine or by people.
# Columns kept in the database for older records but no longer researched (they cost tokens and are not exported).
NOT_RESEARCHED = {"indian_registered_state", "indian_company_status", "indian_incorporation_date", "us_llc_status",
                  "us_formation_date", "relationship_type", "company_description", "employee_range",
                  "email_verification_status", "email_verification_date", "linkedin_company_url",
                  "linkedin_decision_maker_url", "indian_address", "us_address", "recent_activity_evidence"}

RESEARCH_FIELDS = [a for a, _, g in LEAD_FIELDS if g not in ("id", "score", "meta") and a not in NOT_RESEARCHED
                   and a not in ("outreach_status", "assigned_owner", "notes", "next_sales_action")] + [
    "verification_status", "research_limitations"]

# Owned by people. The engine fills them only when empty and never overwrites them.
PROTECTED = {"outreach_status", "assigned_owner", "notes", "next_sales_action"}

URL_LIST_FIELDS = {"relationship_evidence_urls", "source_urls"}

STATUSES = ("VERIFIED", "PARTIALLY VERIFIED", "UNVERIFIED", "DISQUALIFIED")
QUEUE_STATUSES = ("PARTIALLY VERIFIED", "UNVERIFIED")
OUTREACH_STATUSES = ("Not Contacted", "Contacted", "Replied", "Meeting Booked", "Not Interested",
                     "Do Not Contact")
PIPELINE_STAGES = ("New", "Researching", "Contacted", "Engaged", "Meeting Booked", "Proposal Sent",
                   "Won", "Lost")
