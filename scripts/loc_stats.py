#!/usr/bin/env python3
"""Count lines of code Rhythm actually authored, per language, across every repo.

Counts only commits by the configured author, so forks of large upstream projects
(sglang-verl-hpu, autoresearch) contribute his work and not theirs. Vendored,
generated and lock files are excluded -- see SKIP.
"""
import json, os, re, shutil, subprocess, sys, tempfile
from collections import defaultdict
from datetime import datetime, timezone

USER    = os.environ.get("GH_USER", "kaizen-38")
AUTHORS = [a for a in os.environ.get("GH_AUTHORS", "kaizen-38|rarya124@asu.edu").split("|") if a]
# A single commit adding more than this many lines is a vendor/import/restructure,
# not authored code. sglang-verl-hpu has one such commit worth 226k lines.
MAX_COMMIT_ADD = int(os.environ.get("MAX_COMMIT_ADD", "20000"))
# git-hours session heuristic: a gap under SESSION_GAP hours is continuous work;
# a commit that opens a session is credited SESSION_OPEN hours of prior work.
SESSION_GAP  = float(os.environ.get("SESSION_GAP", "2"))
SESSION_OPEN = float(os.environ.get("SESSION_OPEN", "2"))

LANG = {
    ".py":"Python", ".ipynb":"Jupyter", ".ts":"TypeScript", ".tsx":"TypeScript",
    ".js":"JavaScript", ".jsx":"JavaScript", ".mjs":"JavaScript", ".cjs":"JavaScript",
    ".c":"C", ".h":"C", ".cc":"C++", ".cpp":"C++", ".cxx":"C++", ".hpp":"C++",
    ".cu":"CUDA", ".cuh":"CUDA", ".java":"Java", ".go":"Go", ".rs":"Rust",
    ".rb":"Ruby", ".php":"PHP", ".swift":"Swift", ".kt":"Kotlin", ".scala":"Scala",
    ".sh":"Shell", ".bash":"Shell", ".zsh":"Shell", ".ps1":"PowerShell",
    ".sql":"SQL", ".html":"HTML", ".htm":"HTML", ".css":"CSS", ".scss":"CSS",
    ".sass":"CSS", ".less":"CSS", ".vue":"Vue", ".svelte":"Svelte",
    ".tex":"TeX", ".bib":"TeX", ".md":"Markdown", ".mdx":"Markdown",
    ".rst":"reStructuredText", ".yml":"YAML", ".yaml":"YAML", ".toml":"TOML",
    ".json":"JSON", ".proto":"Protobuf", ".dockerfile":"Docker", ".tf":"Terraform",
    ".r":"R", ".m":"MATLAB", ".jl":"Julia", ".pl":"Perl", ".lua":"Lua",
    ".make":"Makefile", ".mk":"Makefile", ".cmake":"CMake", ".gradle":"Gradle",
}
BASENAME_LANG = {"Dockerfile":"Docker", "Makefile":"Makefile", "CMakeLists.txt":"CMake"}

# Vendored / generated / data -- never counted as authored code.
SKIP = re.compile(r"""(^|/)(
      node_modules|vendor|third_party|thirdparty|external|\.venv|venv|env
    | dist|build|out|target|__pycache__|\.next|\.nuxt|\.cache|site-packages
    | migrations|generated|_generated|\.git
  )(/|$)
  | (^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock
    | Pipfile\.lock|composer\.lock|go\.sum|uv\.lock)$
  | \.(min\.js|min\.css|map|lock|png|jpe?g|gif|svg|ico|pdf|zip|tar|gz|bin|so|dylib
    | dll|exe|onnx|pt|pth|ckpt|safetensors|npy|npz|parquet|csv|tsv|woff2?|ttf|eot
    | mp4|mov|wav|mp3)$
""", re.X | re.I)

# Framework -> (manifest regex). Detected from dependency manifests, not LOC:
# "which projects use it" is honest, "lines of React" is not.
FRAMEWORKS = {
    "PyTorch": r"^torch\b", "Transformers": r"^transformers\b", "vLLM": r"^vllm\b",
    "SGLang": r"^sglang\b", "verl": r"^verl\b", "DeepSpeed": r"^deepspeed\b",
    "Ray": r"^ray\b", "TensorFlow": r"^tensorflow\b", "scikit-learn": r"^scikit-learn\b",
    "LangChain": r"^langchain", "LangGraph": r"^langgraph\b", "FastAPI": r"^fastapi\b",
    "Flask": r"^flask\b", "Django": r"^django\b", "Pandas": r"^pandas\b",
    "NumPy": r"^numpy\b", "NetworkX": r"^networkx\b", "Gymnasium": r"^gym",
    "Streamlit": r"^streamlit\b", "Pydantic": r"^pydantic\b", "OpenAI SDK": r"^openai\b",
    "Anthropic SDK": r"^anthropic\b", "React": r"^react$", "Next.js": r"^next$",
    "Express": r"^express$", "TailwindCSS": r"^tailwindcss$", "Vite": r"^vite$",
    "Prisma": r"^@prisma/", "Playwright": r"^(@playwright/|playwright)",
}


def sh(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True).stdout


def lang_of(path):
    base = os.path.basename(path)
    if base in BASENAME_LANG:
        return BASENAME_LANG[base]
    return LANG.get(os.path.splitext(base)[1].lower())


def repos():
    """Every repo he owns. Uses /user/repos when authenticated so private repos
    are included; falls back to the public listing otherwise."""
    out = sh(["gh", "api", "user/repos?per_page=100&affiliation=owner",
              "--jq", ".[] | {name, fork, url: .clone_url, default_branch, private}"])
    if not out.strip() or out.lstrip().startswith('{"message"'):
        out = sh(["gh", "api", f"users/{USER}/repos?per_page=100&type=all",
                  "--jq", ".[] | {name, fork, url: .clone_url, default_branch, private: false}"])
    token = os.environ.get("GH_TOKEN") or sh(["gh", "auth", "token"]).strip()
    seen, rs = set(), []
    for line in out.strip().splitlines():
        r = json.loads(line)
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        if r.get("private") and token:
            r["url"] = r["url"].replace("https://",
                                        f"https://x-access-token:{token}@")
        rs.append(r)
    return rs


def scan_repo(repo, tmp):
    """Return (per-language stats, per-year LOC, dropped commits) for his commits only.

    Hours are estimated with the git-hours session heuristic: consecutive commits
    less than SESSION_GAP apart are one working session and the gap counts as
    worked time; a commit that opens a session is credited SESSION_OPEN. It is an
    estimate from commit timestamps, not tracked time -- see the README note.
    """
    d = os.path.join(tmp, repo["name"])
    clone = subprocess.run(
        ["git", "clone", "--quiet", "--filter=blob:none", "--single-branch", repo["url"], d],
        capture_output=True, text=True)
    if clone.returncode:
        print(f"  ! clone failed: {repo['name']}", file=sys.stderr)
        return {}, {}, []

    args = ["git", "log", "--no-merges", "--numstat", "--format=@@|%H|%aI"]
    for a in AUTHORS:
        args += ["--author", a]
    log = sh(args, cwd=d)

    # Group the numstat rows by commit so an oversized commit can be dropped whole.
    commits, cur = [], None
    for line in log.splitlines():
        if line.startswith("@@|"):
            cur = {"ts": line.split("|")[2], "rows": []}
            commits.append(cur)
            continue
        parts = line.split("\t")
        if len(parts) != 3 or cur is None:
            continue
        add, dele, path = parts
        if add == "-" or SKIP.search(path):
            continue
        lang = lang_of(path)
        if lang:
            cur["rows"].append((int(add), int(dele or 0), lang))

    # git log is newest-first; sessions need chronological order.
    commits.sort(key=lambda c: c["ts"])
    prev = None
    for c in commits:
        t = datetime.fromisoformat(c["ts"])
        gap = (t - prev).total_seconds() / 3600 if prev else None
        c["hours"] = gap if (gap is not None and gap < SESSION_GAP) else SESSION_OPEN
        prev = t

    langs, years, skipped = defaultdict(lambda: defaultdict(float)), defaultdict(int), []
    for c in commits:
        date = c["ts"][:10]
        total = sum(r[0] for r in c["rows"])
        if total > MAX_COMMIT_ADD:
            skipped.append({"repo": repo["name"], "date": date, "added": total})
            continue
        # Split the session time across languages by share of lines touched.
        touched = sum(r[0] + r[1] for r in c["rows"]) or 1
        for a, dl, lang in c["rows"]:
            st = langs[lang]
            st["added"] += a
            st["deleted"] += dl
            st["hours"] += c["hours"] * ((a + dl) / touched)
            if not st.get("first") or date < st["first"]:
                st["first"] = date
            if not st.get("last") or date > st["last"]:
                st["last"] = date
            years[date[:4]] += a
    shutil.rmtree(d, ignore_errors=True)
    return langs, years, skipped


def frameworks(owned):
    """Which frameworks appear in which repos, read from dependency manifests.

    Manifests are found anywhere in the tree (monorepos keep package.json under
    frontend/), and only repos where he actually authored code are considered --
    otherwise a fork's dependencies get credited to him.
    """
    found = defaultdict(set)
    want = ("requirements.txt", "pyproject.toml", "package.json")
    for r in repos():
        if r["name"] not in owned and not r.get("private"):
            continue
        tree = sh(["gh", "api",
                   f"repos/{USER}/{r['name']}/git/trees/{r['default_branch']}?recursive=1",
                   "--jq", '.tree[]? | select(.type=="blob") | .path'])
        paths = [p for p in tree.splitlines()
                 if os.path.basename(p) in want and not SKIP.search(p)]
        for path in paths[:40]:
            txt = sh(["gh", "api", f"repos/{USER}/{r['name']}/contents/{path}",
                      "-H", "Accept: application/vnd.github.raw"])
            if not txt.strip() or txt.lstrip().startswith('{"message"'):
                continue
            if os.path.basename(path) == "package.json":
                try:
                    pkg = json.loads(txt)
                    names = list(pkg.get("dependencies", {})) + list(pkg.get("devDependencies", {}))
                except Exception:
                    names = re.findall(r'"([A-Za-z0-9_@/.\-]+)"\s*:', txt)
            else:
                names = re.findall(r'^\s*["\']?([A-Za-z0-9_@/.\-]+)', txt, re.M)
            for fw, pat in FRAMEWORKS.items():
                if any(re.match(pat, n, re.I) for n in names):
                    found[fw].add(r["name"])
    return found


def main():
    totals, years, dropped, per_repo = (defaultdict(lambda: defaultdict(float)),
                                        defaultdict(int), [], {})
    private_n = set()
    with tempfile.TemporaryDirectory() as tmp:
        for r in repos():
            tag = " (fork)" if r["fork"] else (" (private)" if r.get("private") else "")
            print(f"  scanning {r['name']}{tag}", file=sys.stderr)
            langs, ys, skipped = scan_repo(r, tmp)
            dropped += skipped
            if langs:
                # Private repo names stay out of the committed JSON -- unpublished
                # research repo names would otherwise leak from a public README.
                key = r["name"] if not r.get("private") else "__private__"
                per_repo[key] = per_repo.get(key, 0) + int(sum(v["added"] for v in langs.values()))
                if r.get("private"):
                    private_n.add(r["name"])
            for lang, s in langs.items():
                t = totals[lang]
                t["added"] += s["added"]
                t["deleted"] += s["deleted"]
                t["hours"] += s["hours"]
                if not t.get("first") or s["first"] < t["first"]:
                    t["first"] = s["first"]
                if not t.get("last") or s["last"] > t["last"]:
                    t["last"] = s["last"]
            for y, v in ys.items():
                years[y] += v
    totals = {k: v for k, v in totals.items() if v["added"] > 0}
    for v in totals.values():
        v["added"], v["deleted"] = int(v["added"]), int(v["deleted"])
        v["hours"] = round(v["hours"], 2)
    out = {"languages": {k: dict(v) for k, v in
                         sorted(totals.items(), key=lambda x: -x[1]["added"])},
           "years": dict(sorted(years.items())),
           "per_repo": {(f"{len(private_n)} private repositories" if k == "__private__" else k): v
                        for k, v in sorted(per_repo.items(), key=lambda x: -x[1])},
           "repo_count": len(per_repo) - 1 + len(private_n) if "__private__" in per_repo else len(per_repo),
           "excluded_bulk_commits": dropped,
           "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    if "--with-frameworks" in sys.argv:
        fw = frameworks(set(per_repo) | private_n)
        # Same redaction as per_repo: counts are public, private names are not.
        out["frameworks"] = {
            k: sorted(r for r in v if r not in private_n) +
               ([f"+{n} private"] if (n := len(v & private_n)) else [])
            for k, v in sorted(fw.items(), key=lambda x: -len(x[1]))}
    json.dump(out, open(os.environ.get("OUT", "loc_stats.json"), "w"), indent=2)
    for k, v in out["languages"].items():
        print(f'{k:22} {v["added"]:>8,} lines  {v["hours"]:>7.1f} h  {v["first"]} -> {v["last"]}')
    print("\nby repo:", json.dumps(out["per_repo"], indent=2))
    print("dropped bulk commits:", json.dumps(dropped, indent=2))


if __name__ == "__main__":
    main()
