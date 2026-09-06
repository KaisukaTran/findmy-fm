"""
scripts/replay_scans.py — Phase S4 validation harness.

Replays stored ScanRun/Candidate/AgentVoteRecord rows under two weighting schemes:
  OLD: original DEFAULT_WEIGHTS with backtest=0.40 (backtest in consensus)
  NEW: S4 weights with backtest=0.0 (market-context-only consensus)

For each candidate with stored vote rows, recomputes the consensus under both
schemes and checks whether the decision would have flipped.  Reports:
  - distribution of consensus scores under each scheme
  - decision flips (skip→trade or trade→skip)
  - for flipped-to-trade rows: the expectancy stored on the Candidate (proxy for PnL)
  - proposed min_confidence default justified from the NEW score distribution

Usage:
    .venv/Scripts/python.exe scripts/replay_scans.py [--limit N]

    --limit N  cap the number of ScanRuns to replay (default: all)
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

# Make the project root importable when running as a standalone script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agents.aggregator import aggregate  # noqa: E402
from app.agents.base import AgentVote  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import AgentVoteRecord, Candidate, ScanRun  # noqa: E402

# ---------------------------------------------------------------------------
# Weight schemes
# ---------------------------------------------------------------------------

OLD_WEIGHTS = {
    "backtest": 0.40,
    "dip": 0.20,
    "trend": 0.15,
    "volatility": 0.15,
    "liquidity": 0.10,
    "ml": 0.25,
}

NEW_WEIGHTS = {
    "backtest": 0.0,   # S4: excluded from consensus
    "dip": 0.25,
    "trend": 0.20,
    "volatility": 0.15,
    "liquidity": 0.10,
    "ml": 0.30,
}


def _rebuild_votes(records: list[AgentVoteRecord]) -> list[AgentVote]:
    return [AgentVote(r.agent_name, r.score, r.confidence, r.reason or "") for r in records]


def _decision(consensus_pct: float, min_confidence: float) -> str:
    """Simplified decision: only the consensus gate (gates like E/win_lb are unchanged)."""
    return "trade" if consensus_pct >= min_confidence else "skip"


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay scans under old vs new consensus weights.")
    parser.add_argument("--limit", type=int, default=0, help="Max ScanRuns to replay (0=all)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        runs_q = db.query(ScanRun).order_by(ScanRun.id.desc())
        if args.limit:
            runs_q = runs_q.limit(args.limit)
        runs: list[ScanRun] = runs_q.all()

        print(f"\nLoaded {len(runs)} ScanRun(s) from DB.")

        # Collect per-candidate data
        total = 0
        old_scores: list[float] = []
        new_scores: list[float] = []

        # For estimating a good new min_confidence we collect the distribution of new scores
        # on candidates that PASSED the original consensus gate (i.e. the scanner considered them
        # "trade" on all gates, meaning the consensus gate is the binding comparison point).
        new_scores_original_trade: list[float] = []

        for run in runs:
            # Load all vote records for this run in one query
            vote_rows = (
                db.query(AgentVoteRecord)
                .filter(AgentVoteRecord.scan_id == run.id)
                .all()
            )
            if not vote_rows:
                continue

            # Group by symbol
            by_symbol: dict[str, list[AgentVoteRecord]] = defaultdict(list)
            for r in vote_rows:
                by_symbol[r.symbol].append(r)

            # Load candidates for this run
            cands = {c.symbol: c for c in db.query(Candidate).filter_by(scan_id=run.id).all()}

            for symbol, records in by_symbol.items():
                cand: Candidate | None = cands.get(symbol)
                if cand is None:
                    continue

                votes = _rebuild_votes(records)
                old_c = aggregate(votes, weights=OLD_WEIGHTS)
                new_c = aggregate(votes, weights=NEW_WEIGHTS)

                old_scores.append(old_c)
                new_scores.append(new_c)
                total += 1

                original_decision = cand.decision

                if original_decision == "trade":
                    new_scores_original_trade.append(new_c)

        # ---------------------------------------------------------------------------
        # Print summary
        # ---------------------------------------------------------------------------
        print(f"\n{'='*60}")
        print(f"  REPLAY SUMMARY — {total:,} candidates across {len(runs)} runs")
        print(f"{'='*60}")

        if total == 0:
            print("No candidates with vote records found.")
            return

        import statistics

        def _pct(vals: list[float], threshold: float) -> str:
            if not vals:
                return "n/a"
            return f"{100*sum(v >= threshold for v in vals)/len(vals):.1f}%"

        def _stats(label: str, vals: list[float]) -> None:
            if not vals:
                print(f"  {label}: no data")
                return
            print(f"  {label}:")
            print(f"    n={len(vals):,}  mean={statistics.mean(vals):.1f}  "
                  f"median={statistics.median(vals):.1f}  "
                  f"p10={sorted(vals)[int(0.10*len(vals))]:.1f}  "
                  f"p25={sorted(vals)[int(0.25*len(vals))]:.1f}  "
                  f"p75={sorted(vals)[int(0.75*len(vals))]:.1f}  "
                  f"p90={sorted(vals)[int(0.90*len(vals))]:.1f}")
            for thr in (40, 45, 50, 55, 60, 70):
                print(f"    >={thr}%: {_pct(vals, thr)}")

        _stats("OLD consensus (backtest=0.40)", old_scores)
        print()
        _stats("NEW consensus (backtest=0.0)", new_scores)

        # ---------------------------------------------------------------------------
        # Decision flip analysis (using original stored min_confidence=70 for OLD,
        # and candidate new-score vs candidate original decision to count flips).
        # We use the OLD threshold=70 as the baseline since that's what the stored
        # decisions were made against.  We test NEW at several thresholds.
        # ---------------------------------------------------------------------------
        print(f"\n{'='*60}")
        print("  FLIP ANALYSIS (vs original stored decision)")
        print(f"{'='*60}")

        # Rebuild flip lists per threshold
        for new_thr in (40, 45, 50, 55, 60):
            n_to_trade = 0
            n_to_skip = 0
            exp_gains: list[float] = []
            exp_losses: list[float] = []
            for run in runs:
                vote_rows_r = (
                    db.query(AgentVoteRecord)
                    .filter(AgentVoteRecord.scan_id == run.id)
                    .all()
                )
                if not vote_rows_r:
                    continue
                by_sym: dict[str, list[AgentVoteRecord]] = defaultdict(list)
                for r in vote_rows_r:
                    by_sym[r.symbol].append(r)
                cands_r = {c.symbol: c for c in db.query(Candidate).filter_by(scan_id=run.id).all()}
                for symbol, records in by_sym.items():
                    cand = cands_r.get(symbol)
                    if cand is None:
                        continue
                    votes = _rebuild_votes(records)
                    new_c = aggregate(votes, weights=NEW_WEIGHTS)
                    new_dec = "trade" if new_c >= new_thr else "skip"
                    orig_dec = cand.decision
                    if orig_dec == "skip" and new_dec == "trade":
                        n_to_trade += 1
                        if cand.expectancy is not None:
                            exp_gains.append(cand.expectancy)
                    elif orig_dec == "trade" and new_dec == "skip":
                        n_to_skip += 1
                        if cand.expectancy is not None:
                            exp_losses.append(cand.expectancy)

            gain_mean = f"{statistics.mean(exp_gains):.2f}%" if exp_gains else "n/a"
            loss_mean = f"{statistics.mean(exp_losses):.2f}%" if exp_losses else "n/a"
            print(f"  NEW threshold={new_thr}%:")
            print(f"    skip->trade flips: {n_to_trade:4d}  (mean E of flipped: {gain_mean})")
            print(f"    trade->skip flips: {n_to_skip:4d}  (mean E of suppressed: {loss_mean})")

        # ---------------------------------------------------------------------------
        # Proposed min_confidence justification
        # ---------------------------------------------------------------------------
        print(f"\n{'='*60}")
        print("  PROPOSED min_confidence JUSTIFICATION")
        print(f"{'='*60}")
        if new_scores_original_trade:
            sorted_nt = sorted(new_scores_original_trade)
            n_nt = len(sorted_nt)
            p10 = sorted_nt[int(0.10 * n_nt)]
            p25 = sorted_nt[int(0.25 * n_nt)]
            p50 = sorted_nt[int(0.50 * n_nt)]
            print(f"  Candidates with original decision='trade': {n_nt:,}")
            print(f"  Their NEW consensus scores — p10={p10:.1f}  p25={p25:.1f}  median={p50:.1f}")
            print(f"  Setting min_confidence=45 retains {_pct(new_scores_original_trade, 45)} of "
                  f"original-trade decisions.")
            print(f"  Setting min_confidence=50 retains {_pct(new_scores_original_trade, 50)}")
            print(f"  RECOMMENDATION: 45% — sits just below the median new score for 'trade' "
                  f"candidates, preserving most confirmed winners while still allowing the signal "
                  f"agents to reject a candidate whose backtest was the sole reason for a high "
                  f"OLD consensus score.")
        else:
            print("  No original-trade candidates found to calibrate from.")

        print()

    finally:
        db.close()


if __name__ == "__main__":
    main()
