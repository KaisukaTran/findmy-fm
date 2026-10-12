"""EXPLORATORY (outside the pre-registered family D1..D5): D3 variants on the same per-session start set.
    E1 = D1 + D3 (tp_step 0, deep exit 30d @ >=5 fills)
    E2 = D3 with >=8 fills     E3 = D3 with >=10 fills (full ladder)
    E4 = D3 with age >= 45d    E5 = D3 with age >= 20d      (all @ >=5 fills unless stated)
Writes an npz in the same format as per_session.py (A0 included for pairing)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import deep_engine as eng  # noqa: E402
import per_session as ps  # noqa: E402

eng.VARIANTS.update({
    "E1": dict(tp_step_pct=0.0, deep_exit_days=30.0),
    "E2": dict(deep_k=8, deep_exit_days=30.0),
    "E3": dict(deep_k=10, deep_exit_days=30.0),
    "E4": dict(deep_exit_days=45.0),
    "E5": dict(deep_exit_days=20.0),
})
ps.VARS = ["A0", "E1", "E2", "E3", "E4", "E5"]
ps.CFGS = {(v, b): ps.make_cfg(v, b == "pess") for v in ps.VARS for b in ps.BOUNDS}

if __name__ == "__main__":
    ps.main()
