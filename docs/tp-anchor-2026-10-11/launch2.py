import sys
w, nw = int(sys.argv[1]), int(sys.argv[2])
jobs = []
for cfg in ("paper_cov30_n5", "paper_cov1_n5"):
    for s in range(10):
        for a in ("avg", "entry", "max"):
            for b in ("pess", "opt"):
                jobs.append(f"{cfg}|{a}|{b}|{s}")
seen = {}; mine = []
for j in jobs:
    key = tuple(j.split("|")[0::3]); seen.setdefault(key, len(seen))
    if seen[key] % nw == w: mine.append(j)
print(",".join(mine))
