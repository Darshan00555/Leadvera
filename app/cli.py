"""Admin CLI: `python -m app.cli <command>`.

  init-db                          create tables
  create-user EMAIL --role admin   create a user (prompts for password)
  run-now [--markets GB,AU] [--locations Chennai] [--industries SaaS] [--assignments 5] [--no-revisit]
                                   queue a research run (the worker picks it up); options narrow its focus
  send-report [RUN_ID]             build and email the report for a run (default: latest)
  test-email                       send a test email with the saved SMTP settings
  import-india CSV --source URL    import MCA / data.gov.in company master data
  export-xlsx OUT.xlsx             write the master workbook
  refresh-web-tier                 re-check every research-queue item against the web-verified rule
"""
import argparse
import getpass
import sys
from datetime import date

from . import appsettings, excel, mailer, scheduler
from .bootstrap import bootstrap
from .db import SessionLocal
from .models import Run, User
from .security import hash_password, password_problems


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    p = sub.add_parser("create-user"); p.add_argument("email"); p.add_argument("--role", default="admin",
                                                                               choices=["admin", "analyst", "sales"])
    p.add_argument("--name", default="")
    p = sub.add_parser("run-now")
    p.add_argument("--markets", default="", help="US, GB/UK, AU, NZ, EU, EEA or EU/EEA country codes")
    p.add_argument("--us-states", default="")
    p.add_argument("--locations", default="", help="Indian cities or states")
    p.add_argument("--industries", default="")
    p.add_argument("--assignments", type=int)
    p.add_argument("--no-revisit", action="store_true")
    p.add_argument("--max-leads", type=int, help="stop this run after this many new verified + web-verified leads")
    p = sub.add_parser("send-report"); p.add_argument("run_id", nargs="?", type=int)
    sub.add_parser("test-email")
    p = sub.add_parser("import-india"); p.add_argument("csv"); p.add_argument("--source", required=True)
    p = sub.add_parser("export-xlsx"); p.add_argument("out")
    sub.add_parser("refresh-web-tier")
    a = ap.parse_args(argv)

    bootstrap()
    db = SessionLocal()
    try:
        if a.cmd == "init-db":
            print("database ready")
        elif a.cmd == "create-user":
            pw = getpass.getpass("Password: ")
            if probs := password_problems(pw):
                sys.exit("Password needs " + ", ".join(probs))
            db.add(User(email=a.email.lower(), name=a.name or a.email, role=a.role, password_hash=hash_password(pw)))
            db.commit()
            print(f"created {a.role} {a.email}")
        elif a.cmd == "run-now":
            from .research.planner import clean_targets, describe_targets
            split = lambda v: [x for x in v.split(",") if x.strip()]  # noqa: E731
            t = clean_targets(split(a.markets), split(a.us_states), split(a.locations), split(a.industries),
                              a.assignments, not a.no_revisit, a.max_leads)
            if t and not t.get("markets"):
                t["markets"] = appsettings.get(db, "target_markets") or ["US"]
            r = Run(run_date=date.today(), trigger="manual", status="queued", targets=t or None)
            db.add(r); db.commit()
            print(f"queued run {r.id}" + (f" - focus: {describe_targets(t)}" if t else ""))
        elif a.cmd == "send-report":
            run = db.get(Run, a.run_id) if a.run_id else db.query(Run).order_by(Run.id.desc()).first()
            if not run:
                sys.exit("no runs yet")
            scheduler.send_run_report(db, run, appsettings.get_all(db, include_secrets=True))
            print(f"report for run {run.id} sent")
        elif a.cmd == "test-email":
            s = appsettings.get_all(db, include_secrets=True)
            mailer.send(db, "test", s["email_recipients"], "Test email",
                        "SMTP settings work.", "<p>SMTP settings work.</p>")
            print("sent")
        elif a.cmd == "import-india":
            from .india_import import import_csv
            with open(a.csv, "rb") as fh:
                print(import_csv(db, fh, a.source))
        elif a.cmd == "refresh-web-tier":
            from .ingest import refresh_web_tier
            print(f"{refresh_web_tier(db)} research-queue items newly web-verified")
        elif a.cmd == "export-xlsx":
            with open(a.out, "wb") as fh:
                fh.write(excel.build_workbook(db))
            print(f"wrote {a.out}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
