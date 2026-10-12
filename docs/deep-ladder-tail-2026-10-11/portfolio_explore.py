"""EXPLORATORY portfolio runs (outside the pre-registered family). Same usage as portfolio.py.
    E1 = D1+D3 (tp_step 0 + deep exit 30d @ >=5 fills)    E5 = D3 with a 20-day clock (@ >=5 fills)"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import deep_engine as eng  # noqa: E402
import portfolio  # noqa: E402

eng.VARIANTS.update({
    "E1": dict(tp_step_pct=0.0, deep_exit_days=30.0),
    "E5": dict(deep_exit_days=20.0),
})

if __name__ == "__main__":
    portfolio.main()
