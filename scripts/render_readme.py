#!/usr/bin/env python3
"""Fill the auto-generated blocks in README.md from loc_stats.json + the GitHub API."""
import json, os, re, subprocess
from datetime import datetime

USER   = os.environ.get("GH_USER", "kaizen-38")
README = os.environ.get("README", "README.md")
BAR    = 25


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def block(md, tag, body):
    """Replace everything between <!--START:tag--> and <!--END:tag-->."""
    pat = re.compile(rf"(<!--START:{tag}-->).*?(<!--END:{tag}-->)", re.S)
    if not pat.search(md):
        raise SystemExit(f"marker {tag} missing from {README}")
    return pat.sub(lambda m: f"{m.group(1)}\n{body}\n{m.group(2)}", md)


def month(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%b %Y")


def hrs(h):
    return f"{int(h):,}h" if h >= 10 else f"{h:.1f}h"


def loc_block(stats):
    langs = stats["languages"]
    total = sum(v["added"] for v in langs.values())
    hours = sum(v.get("hours", 0) for v in langs.values())
    span = (min(v["first"] for v in langs.values()), max(v["last"] for v in langs.values()))
    out = [f"From: {month(span[0])} - To: {month(span[1])}", "",
           f"Total: {total:,} lines over ~{hrs(hours)} across "
           f"{stats.get('repo_count', len(stats['per_repo']))} repositories", ""]
    w = max(len(k) for k in langs)
    hw = max(len(hrs(v.get("hours", 0))) for v in langs.values())
    for name, v in langs.items():
        pct = v["added"] / total * 100
        filled = round(pct / 100 * BAR)
        out.append(f"{name:<{w}}  {v['added']:>7,} lines  "
                   f"{hrs(v.get('hours', 0)):>{hw}}  "
                   f"{'>' * filled}{'-' * (BAR - filled)}  {pct:5.2f} %  "
                   f"{month(v['first'])} -> {month(v['last'])}")
    return "```text\n" + "\n".join(out) + "\n```"


def year_block(stats):
    years = stats["years"]
    top = max(years.values())
    rows = [f"{y}  {v:>7,} lines  {'#' * round(v / top * 40)}" for y, v in years.items()]
    return "```text\n" + "\n".join(rows) + "\n```"


GROUPS = [
    ("Post-training & ML", ["PyTorch", "TensorFlow", "Transformers", "verl", "slime",
                            "MILES", "SGLang", "vLLM", "DeepSpeed", "Ray", "scikit-learn"]),
    ("Agents & LLM tooling", ["LangChain", "LangGraph", "OpenAI SDK", "Anthropic SDK",
                              "Playwright", "Pydantic"]),
    ("Web & product",   ["React", "Next.js", "Vite", "TailwindCSS", "FastAPI", "Flask",
                         "Django", "Express", "Prisma", "Streamlit"]),
    ("Data & scientific", ["Pandas", "NumPy", "NetworkX", "Gymnasium"]),
]


def framework_block(stats):
    """Grouped so the specialised tools read above the ubiquitous ones."""
    fw = stats.get("frameworks", {})
    if not fw:
        return "_No framework manifests detected._"
    rows = ["| | Detected in my repositories |", "|---|---|"]
    placed = set()
    for label, members in GROUPS:
        hits = [(m, fw[m]) for m in members if m in fw]
        placed |= {m for m, _ in hits}
        if hits:
            cells = ", ".join(f"**{m}** ({len(r)})" for m, r in hits)
            rows.append(f"| **{label}** | {cells} |")
    rest = [(k, v) for k, v in fw.items() if k not in placed]
    if rest:
        rows.append("| **Other** | " +
                    ", ".join(f"**{k}** ({len(v)})" for k, v in rest) + " |")
    rows.append("")
    rows.append("<sub>Number in brackets is how many of my repositories declare it.</sub>")
    return "\n".join(rows)


def stats_block():
    repos = json.loads(sh(["gh", "api", f"users/{USER}/repos?per_page=100&type=owner",
                           "--jq", "[.[] | {stargazers_count, forks_count, open_issues_count, fork}]"]) or "[]")
    own = [r for r in repos if not r["fork"]]
    def count(q):
        r = sh(["gh", "api", f"search/issues?q={q}&per_page=1", "--jq", ".total_count"]).strip()
        return r or "0"
    rows = [
        ("🌟 Total stars", sum(r["stargazers_count"] for r in own)),
        ("🍴 Total forks", sum(r["forks_count"] for r in own)),
        ("📦 Public repositories", len(own)),
        ("🔄 Pull requests opened", count(f"type:pr+author:{USER}")),
        ("📝 Issues reported", count(f"type:issue+author:{USER}")),
    ]
    return ("<table>\n" +
            "\n".join(f"  <tr><td>{k}</td><td><strong>{v}</strong></td></tr>" for k, v in rows) +
            "\n</table>")


def main():
    stats = json.load(open(os.environ.get("STATS", "loc_stats.json")))
    md = open(README).read()
    md = block(md, "LOC", loc_block(stats))
    md = block(md, "YEARS", year_block(stats))
    md = block(md, "FRAMEWORKS", framework_block(stats))
    md = block(md, "GHSTATS", stats_block())
    md = re.sub(r"(_Regenerated ).*?(_)", rf"\g<1>{datetime.now().strftime('%d %b %Y')}\g<2>", md)
    open(README, "w").write(md)
    print("README.md updated")


if __name__ == "__main__":
    main()
