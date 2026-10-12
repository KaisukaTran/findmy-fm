"""Print the comma-joined job list for worker w of nw. Seed-major so partial results stay paired.
    python launch.py w nw [primary|n5]"""
import sys

w, nw = int(sys.argv[1]), int(sys.argv[2])
part = sys.argv[3] if len(sys.argv) > 3 else "primary"
V = ("A0", "D1", "D2", "D3", "D4", "D5")
jobs = []
if part == "primary":
    spec = [("cov1", 20), ("cov30", 20)]
else:
    spec = [("cov30_n5", 10), ("cov1_n5", 10)]
for cfg, nseed in spec:
    for s in range(nseed):
        for v in V:
            for b in ("pess", "opt"):
                jobs.append(f"{cfg}|{v}|{b}|{s}")
mine, seen = [], {}
for j in jobs:
    key = (j.split("|")[0], j.split("|")[3])
    seen.setdefault(key, len(seen))
    if seen[key] % nw == w:
        mine.append(j)
print(",".join(mine))
