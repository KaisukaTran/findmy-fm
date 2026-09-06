import json
from datetime import datetime
from pathlib import Path

def generate_audit_id():
    return datetime.utcnow().strftime("audit-%Y-%m-%dT%H-%M")

def write_audit(audit: dict):
    audit_id = audit["audit_metadata"]["audit_id"]

    base = Path("audits/runs")
    base.mkdir(parents=True, exist_ok=True)

    json_path = base / f"{audit_id}.json"
    html_path = base / f"{audit_id}.html"

    with open(json_path, "w") as f:
        json.dump(audit, f, indent=2)

    return json_path, html_path
