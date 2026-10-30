"""Import MCA / data.gov.in company master data (CSV) into the local lookup table."""
import csv
import io

from . import rules as R
from .models import IndianCompany

COLUMN_ALIASES = {
    "cin": ("corporate_identification_number", "cin", "llpin", "cin/llpin", "company_cin"),
    "name": ("company_name", "companyname", "name", "llp_name"),
    "status": ("company_status", "companystatus", "status", "llp_status"),
    "state": ("registered_state", "companystatecode", "state", "company_state_code"),
    "incorporation_date": ("date_of_registration", "companyregistrationdate_date", "date_of_incorporation",
                           "incorporation_date", "dateofincorporation"),
    "company_class": ("company_class", "companyclass", "class"),
    "activity": ("principal_business_activity_as_per_cin", "companyindustrialclassification",
                 "principal_business_activity", "industrial_class"),
}


def _resolve(headers):
    norm = {h.strip().lower().replace(" ", "_"): h for h in headers}
    out = {}
    for field, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            if a in norm:
                out[field] = norm[a]
                break
    if "cin" not in out or "name" not in out:
        raise ValueError(f"CSV needs a CIN/LLPIN column and a company-name column; found {list(headers)}")
    return out


def import_csv(db, fileobj, source_url, batch=2000):
    text = io.TextIOWrapper(fileobj, encoding="utf-8-sig", errors="replace", newline="")
    reader = csv.DictReader(text)
    cols = _resolve(reader.fieldnames or [])
    inserted = updated = skipped = 0
    pending = {}

    def flush():
        nonlocal inserted, updated
        existing = {c.cin: c for c in db.query(IndianCompany).filter(IndianCompany.cin.in_(list(pending)))}
        for cin, vals in pending.items():
            row = existing.get(cin)
            if row:
                for k, v in vals.items():
                    setattr(row, k, v)
                updated += 1
            else:
                db.add(IndianCompany(cin=cin, **vals))
                inserted += 1
        db.commit()
        pending.clear()

    for rec in reader:
        cin = (rec.get(cols["cin"]) or "").strip().upper()
        name = (rec.get(cols["name"]) or "").strip()
        if not R.valid_indian_id(cin) or not name:
            skipped += 1
            continue
        pending[cin] = {"name": name, "norm_name": R.norm_name(name), "source_url": source_url,
                        **{f: (rec.get(cols[f]) or "").strip()[:500] for f in
                           ("status", "state", "incorporation_date", "company_class", "activity") if f in cols}}
        if len(pending) >= batch:
            flush()
    if pending:
        flush()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}
