from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from audit_contract import validate_audit_contract
import json

BASE_DIR = Path(__file__).parent

env = Environment(
    loader=FileSystemLoader(BASE_DIR)
)

def render_html(audit: dict) -> str:
    template = env.get_template("audit.html.j2")
    return template.render(audit=audit)

if __name__ == "__main__":
    with open(BASE_DIR.parent / "runs" / "audit_sample.json") as f:
        audit = json.load(f)

    validate_audit_contract(audit)

    html = render_html(audit)

    output_path = BASE_DIR.parent / "runs" / "audit_sample.html"
    with open(output_path, "w") as f:
        f.write(html)

    print(f"✅ Audit HTML rendered successfully: {output_path}")
