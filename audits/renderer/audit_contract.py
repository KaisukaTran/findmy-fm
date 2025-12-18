REQUIRED_TOP_LEVEL_KEYS = [
    "audit_metadata",
    "day_contract",
    "architecture_snapshot",
    "rule_checks",
    "change_impact",
    "audit_verdict"
]

def validate_audit_contract(audit: dict):
    missing = [
        k for k in REQUIRED_TOP_LEVEL_KEYS
        if k not in audit
    ]
    if missing:
        raise ValueError(
            f"Audit contract invalid. Missing sections: {missing}"
        )
