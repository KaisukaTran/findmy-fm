"""Print the comma-joined job list for worker `w` of `nw` (round-robin over seeds)."""
import sys

w, nw = int(sys.argv[1]), int(sys.argv[2])
seeds = int(sys.argv[3]) if len(sys.argv) > 3 else 20
jobs = []
# primary: paper config, all 3 variants x 2 bounds, seed-major so partial results stay paired
for cfg, nseed, anchors in (("paper_cov1", seeds, ("avg", "entry", "max")),
                            ("paper_cov30", seeds, ("avg", "entry", "max")),
                            ("rep_cov1_floor0", 10, ("avg",))):
    for s in range(nseed):
        for a in anchors:
            for b in ("pess", "opt"):
                jobs.append(f"{cfg}|{a}|{b}|{s}")
mine = []
# assign by (cfg, seed) so paired runs of a seed land in the same worker
seen = {}
for j in jobs:
    key = tuple(j.split("|")[0::3])
    seen.setdefault(key, len(seen))
    if seen[key] % nw == w:
        mine.append(j)
print(",".join(mine))
