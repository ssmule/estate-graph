#!/usr/bin/env python3
"""Estate Graph generator.

Scans a projects root (default: this script's parent's parent, i.e. ~/Work/projects)
and emits a node/edge graph model of the *current state* of every top-level
directory: git health, tech stack, owning org, and a derived category.

Outputs (next to this script):
  estate.json     portable graph model {meta, nodes, edges}
  estate-data.js  same model as `window.ESTATE = {...}` for the offline HTML view
  estate.cypher   Neo4j load script (he said "like neo4j")

The static index.html + vendor/cytoscape.min.js render it offline. Re-run any time:
  python3 generate.py            # scans the parent projects dir
  python3 generate.py /some/dir  # scan a different root
  python3 generate.py --selfcheck

Stdlib only. Deterministic (sorted). No network calls.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SELF_DIRNAME = os.path.basename(HERE)
# This tool lives in its own dir; scan the estate explicitly (override with argv[1]).
DEFAULT_ROOT = os.path.expanduser("~/Work/projects")
NOW = int(time.time())
DAY = 86400


def load_config() -> dict:
    """Confidential identifiers (employer org name, username) live OUTSIDE this
    source — in an untracked estate.config.json — so generate.py itself is safe to
    publish. Generic, literal-free defaults apply when the file is absent."""
    cfg = {
        "private_org_match": "",                  # substring in a remote URL → "private" org
        "private_org_key": "Private",             # internal org value for matched repos
        "private_org_anon_id": "enterprise",      # public-build replacement id
        "private_org_anon_label": "Enterprise (private)",
        "forbidden_in_public": ["/Users/"],       # tokens that must never reach a public build
    }
    p = os.path.join(HERE, "estate.config.json")
    if os.path.exists(p):
        try:
            with open(p) as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"warning: bad estate.config.json ({e}); using defaults", file=sys.stderr)
    return cfg


CONFIG = load_config()

# Dirs we never descend into when sampling files / counting.
SKIP_DIRS = {
    ".git", "node_modules", "target", "build", "dist", ".venv", "venv",
    "__pycache__", ".gradle", ".idea", ".mvn", "out", ".next", ".cache",
    "vendor", "coverage", ".terraform",
}

# extension -> language label (linguist-lite). First-class intent languages only.
EXT_LANG = {
    ".java": "Java", ".kt": "Kotlin", ".groovy": "Groovy", ".scala": "Scala",
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
    ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".cs": "C#",
    ".tf": "Terraform", ".sh": "Shell", ".bash": "Shell",
    ".sql": "SQL", ".esql": "ESQL", ".xsl": "XSLT", ".xslt": "XSLT",
    ".dwl": "DataWeave", ".yml": "YAML", ".yaml": "YAML",
    ".md": "Markdown", ".tpl": "Config",
}

# Ordered keyword rules -> category. First match wins.
# ponytail: keyword categorizer; if a repo lands in the wrong bucket, tweak a rule
# here rather than special-casing the repo elsewhere.
CATEGORIES = [
    ("AI / ML / Agents", r"^(ai[-_]|agents?[-_]|ainb|lang[-_]?(chain|graph)|graph-rag|"
                          r"vector-db|claude|copilot|openrouter|huggingface|prompt-|"
                          r"loop-engineering|token-opt|sdd-speckit|lite-panda|multica|"
                          r"brightcloud|gh-aw)"),
    ("Mule / ESB", r"(mule|aml-mule|donington|esb)"),
    ("Apigee / API Mgmt", r"(apigee|apim|graphql-docs|asyncapi|backstage)"),
    ("IIB / ACE / Transform", r"(iib|wmb|ace|xi52|xb62|b2b-messagetransform|datapower|adapter)"),
    ("MQ / Messaging / Eventing", r"(wmq|websphere-mq|messaging|kafka|asb|azure-messaging|"
                                   r"events-platform|replay-service)"),
    ("Integration Infra", r"^integration-(.*-)?infra|^integration-(infra|ops|templates|"
                            r"test-automation|catalogue|updates|utilities)|file-gateway|"
                            r"app-gateway|adapter-wms|enterprise-services"),
    ("Microservices", r"(-service$|^article-|^audit-|^exception-|^rda-|^security-|"
                        r"^store-stock|^lcp-|^location|^reservation)"),
    ("DataStage / Data", r"(datastage|integration-ds|mongodb|spark|^ray$|kafka-streams)"),
    ("Cloud / Azure / Observability", r"(azure|cloud-|apm|opentelemetry|cloudflare|"
                                        r"landing-zone|app-gateway)"),
    ("Automation / CICD / Quality", r"(automation|cicd|monthly-patching|repo-maintanence|"
                                      r"software-upgrades|devsecops|playwright|cucumber|locust|"
                                      r"enterprise-quality|design-authority)"),
    ("Platform / Control Plane", r"(pe-repos|integration-modernization|integration-control|"
                                  r"dx-reports|meeting-notes|profile|homebrew-tap|"
                                  r"cloudintegration-templates|integration-messaging)"),
    ("Language / Framework Sandbox", r"^(java|go|rust|kotlin|micronaut|fastapi|meteor|meiteor|"
                                       r"electron|typescripts?_?project|java[-_]|rust_project|"
                                       r"system_design|newproject|zero|git$)"),
    ("Personal", r"(personal-site|^profile$|mac-health|vsquaretrader|accio|stackoverflow|"
                  r"incident-dumps|repo-to-portfolio)"),
]

ORG_LABELS = {
    CONFIG["private_org_key"]: CONFIG["private_org_key"],
    CONFIG["private_org_anon_id"]: CONFIG["private_org_anon_label"],
    "ssmule": "ssmule (personal)",
    "external": "External / OSS",
    "local-only": "Local-only (no remote)",
    "not-git": "Not a git repo",
}

# --- public-build sanitization --------------------------------------------
# Orgs whose repo *names* are employer-confidential or ambiguous -> anonymized
# in the public build. Public/OSS repos (ssmule, external) keep their real names.
CONFIDENTIAL_ORGS = {CONFIG["private_org_key"], "local-only", "not-git"}
CAT_CODE = {
    "AI / ML / Agents": "AI", "Mule / ESB": "MULE", "Apigee / API Mgmt": "APIM",
    "IIB / ACE / Transform": "ACE", "MQ / Messaging / Eventing": "MQ",
    "Integration Infra": "INFRA", "Microservices": "SVC", "DataStage / Data": "DATA",
    "Cloud / Azure / Observability": "CLOUD", "Automation / CICD / Quality": "CICD",
    "Platform / Control Plane": "PLAT", "Language / Framework Sandbox": "LAB",
    "Personal": "PERS", "Other": "OTH",
}
# Strings that must NEVER appear in a published payload (hard gate, see build_public).
FORBIDDEN_IN_PUBLIC = tuple(CONFIG["forbidden_in_public"])


def run_git(repo: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip()
    except Exception:
        return ""


# Build-marker files give a strong "intent" signal that beats a raw file count
# (a Micronaut service's .java lives deep under src/main/java; shallow scripts
# would otherwise win). Each marker adds weight to its language.
def _marker_boosts(path: str, files: list[str], counts: dict) -> None:
    for f in files:
        if f == "mule-artifact.json":
            counts["Mule"] = counts.get("Mule", 0) + 100
        elif f == "pom.xml":
            try:
                with open(os.path.join(path, f), "r", errors="ignore") as fh:
                    is_mule = "mule" in fh.read(4000).lower()
            except Exception:
                is_mule = False
            counts["Mule" if is_mule else "Java"] = \
                counts.get("Mule" if is_mule else "Java", 0) + 80
        elif f.startswith("build.gradle"):
            counts["Java"] = counts.get("Java", 0) + 60
        elif f == "go.mod":
            counts["Go"] = counts.get("Go", 0) + 60
        elif f == "Cargo.toml":
            counts["Rust"] = counts.get("Rust", 0) + 60
        elif f in ("pyproject.toml", "requirements.txt", "setup.py"):
            counts["Python"] = counts.get("Python", 0) + 40
        elif f == "tsconfig.json":
            counts["TypeScript"] = counts.get("TypeScript", 0) + 40


def detect_stack(path: str) -> tuple[str, int]:
    """Return (dominant language label, files_sampled) via a bounded ext histogram
    plus build-marker boosts. Walks to depth 6 so src/main/java is reached."""
    counts: dict[str, int] = {}
    sampled = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        depth = root[len(path):].count(os.sep)
        if depth > 6:
            dirs[:] = []
            continue
        _marker_boosts(root, files, counts)
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            lang = EXT_LANG.get(ext)
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
            sampled += 1
            if sampled >= 8000:  # ponytail: cap the walk; bigger trees don't change the verdict
                break
        if sampled >= 8000:
            break
    if not counts:
        return ("(empty / docs)", sampled)
    # Prefer a "real" code language over doc/config noise when both present.
    secondary = {"YAML", "Markdown", "Config"}
    code = {k: v for k, v in counts.items() if k not in secondary}
    pool = code or counts
    best = max(sorted(pool.items()), key=lambda kv: kv[1])[0]
    return (best, sampled)


def categorize(name: str, remote: str) -> str:
    low = name.lower()
    for label, pat in CATEGORIES:
        if re.search(pat, low):
            return label
    if "digitalinnovation" in remote.lower():
        return "Integration Infra"
    return "Other"


def org_of(remote: str, is_git: bool) -> str:
    if not is_git:
        return "not-git"
    r = remote.lower()
    if not r:
        return "local-only"
    if CONFIG["private_org_match"] and CONFIG["private_org_match"] in r:
        return CONFIG["private_org_key"]
    if "ssmule" in r:
        return "ssmule"
    return "external"


def variant_stem(name: str) -> str:
    """Normalize a repo name to detect near-duplicate variants.
    ponytail: cheap stem match; surfaces 'you have N copies of X'."""
    s = name.lower()
    s = re.sub(r"\.(repos|bak|backup)$", "", s)
    s = re.sub(r"[-_]?(main|old|backup|archive|work|copy|new|v?\d+)$", "", s)
    s = re.sub(r"[-_](main|old|backup|archive|work)[-_]?", "-", s)
    return s.strip("-_")


def classify(is_git: bool, dirty: bool, ahead: int, behind: int, age_days) -> str:
    """Single attention-priority status for colouring/filtering. Highest risk wins."""
    if not is_git:
        return "no-git"
    if ahead and behind:
        return "diverged"      # local & remote both moved — needs reconcile
    if dirty:
        return "dirty"         # uncommitted work on disk
    if ahead:
        return "unpushed"      # committed but not pushed — work at risk
    if age_days is not None and age_days > 90:
        return "stale"
    return "clean"


def scan(root: str) -> dict:
    projects = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path) or name.startswith("."):
            continue
        if name == SELF_DIRNAME:  # don't graph ourselves
            continue
        is_git = os.path.isdir(os.path.join(path, ".git"))
        remote = run_git(path, "config", "--get", "remote.origin.url") if is_git else ""
        branch = run_git(path, "rev-parse", "--abbrev-ref", "HEAD") if is_git else ""
        if branch == "HEAD":
            branch = "detached"
        last_ct = run_git(path, "log", "-1", "--format=%ct") if is_git else ""
        last_epoch = int(last_ct) if last_ct.isdigit() else 0
        dirty = bool(run_git(path, "status", "--porcelain")) if is_git else False
        ahead = behind = 0
        if is_git:
            a = run_git(path, "rev-list", "--count", "@{upstream}..HEAD")
            b = run_git(path, "rev-list", "--count", "HEAD..@{upstream}")
            ahead = int(a) if a.isdigit() else 0
            behind = int(b) if b.isdigit() else 0
        stack, sampled = detect_stack(path)
        org = org_of(remote, is_git)
        category = categorize(name, remote)
        age_days = (NOW - last_epoch) // DAY if last_epoch else None
        status = classify(is_git, dirty, ahead, behind, age_days)
        projects.append({
            "name": name,
            "is_git": is_git,
            "remote": remote,
            "org": org,
            "branch": branch,
            "last_epoch": last_epoch,
            "last_commit": datetime.fromtimestamp(last_epoch, timezone.utc).strftime("%Y-%m-%d")
                            if last_epoch else "",
            "age_days": age_days,
            "dirty": dirty,
            "ahead": ahead,
            "behind": behind,
            "status": status,
            "stack": stack,
            "files_sampled": sampled,
            "category": category,
            "stem": variant_stem(name),
            "path": path,
        })
    return build_model(root, projects)


def build_model(root: str, projects: list[dict]) -> dict:
    nodes, edges = [], []
    orgs, cats, stacks = {}, {}, {}

    def hub(bucket: dict, kind: str, key: str, label: str):
        nid = f"{kind}:{key}"
        if nid not in bucket:
            bucket[nid] = {"data": {"id": nid, "label": label, "kind": kind, "count": 0}}
        bucket[nid]["data"]["count"] += 1
        return nid

    for p in projects:
        pid = f"project:{p['name']}"
        # activity score 0..1 for node sizing (recent = larger)
        if p["age_days"] is None:
            recency = 0.15
        else:
            recency = max(0.15, 1.0 - min(p["age_days"], 365) / 365.0)
        nodes.append({"data": {
            "id": pid, "label": p["name"], "kind": "project",
            "org": p["org"], "category": p["category"], "stack": p["stack"],
            "branch": p["branch"], "remote": p["remote"], "dirty": p["dirty"],
            "ahead": p["ahead"], "behind": p["behind"], "status": p["status"],
            "is_git": p["is_git"], "last_commit": p["last_commit"],
            "age_days": p["age_days"] if p["age_days"] is not None else -1,
            "recency": round(recency, 3), "path": p["path"],
        }})
        oid = hub(orgs, "org", p["org"], ORG_LABELS.get(p["org"], p["org"]))
        cid = hub(cats, "category", p["category"], p["category"])
        sid = hub(stacks, "stack", p["stack"], p["stack"])
        edges.append({"data": {"id": f"e-org-{p['name']}", "source": pid, "target": oid, "rel": "IN_ORG"}})
        edges.append({"data": {"id": f"e-cat-{p['name']}", "source": pid, "target": cid, "rel": "IN_CATEGORY"}})
        edges.append({"data": {"id": f"e-stk-{p['name']}", "source": pid, "target": sid, "rel": "USES_STACK"}})

    # VARIANT_OF edges between projects sharing a stem (duplicates / -main / -old / v2)
    by_stem: dict[str, list[str]] = {}
    for p in projects:
        by_stem.setdefault(p["stem"], []).append(p["name"])
    for stem, members in sorted(by_stem.items()):
        if len(members) < 2:
            continue
        members = sorted(members)
        base = members[0]
        for other in members[1:]:
            edges.append({"data": {
                "id": f"e-var-{other}", "source": f"project:{other}",
                "target": f"project:{base}", "rel": "VARIANT_OF",
            }})

    nodes.extend(orgs.values())
    nodes.extend(cats.values())
    nodes.extend(stacks.values())

    git_repos = sum(1 for p in projects if p["is_git"])
    dirty = sum(1 for p in projects if p["dirty"])
    stale = sum(1 for p in projects if p["age_days"] is not None and p["age_days"] > 90)
    unpushed = sum(1 for p in projects if p["ahead"] > 0)
    diverged = sum(1 for p in projects if p["ahead"] > 0 and p["behind"] > 0)
    attention = sum(1 for p in projects if p["status"] in ("diverged", "dirty", "unpushed", "stale"))
    meta = {
        "root": root,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "total_projects": len(projects),
        "git_repos": git_repos,
        "non_git": len(projects) - git_repos,
        "dirty": dirty,
        "unpushed": unpushed,
        "diverged": diverged,
        "stale_90d": stale,
        "attention": attention,
        "categories": sorted({p["category"] for p in projects}),
        "orgs": {v["data"]["label"]: v["data"]["count"] for v in orgs.values()},
        "stacks": sorted({p["stack"] for p in projects}),
        "variant_groups": {s: m for s, m in by_stem.items() if len(m) > 1},
        # org→colour injected here (not hardcoded in index.html) so the private
        # org name never appears as a literal in the published template.
        "org_colors": {CONFIG["private_org_key"]: "#58a6ff",
                       CONFIG["private_org_anon_id"]: "#58a6ff"},
    }
    return {"meta": meta, "nodes": nodes, "edges": edges}


def to_cypher(model: dict) -> str:
    """Neo4j load script. Idempotent via MERGE."""
    lines = ["// Estate Graph — generated %s" % model["meta"]["generated_at"],
             "// Run:  cat estate.cypher | cypher-shell -u neo4j -p <pw>",
             "// Reset: MATCH (n) WHERE n.estate = true DETACH DELETE n;",
             ""]

    def esc(v) -> str:
        return json.dumps(v if v is not None else "")

    for n in model["nodes"]:
        d = n["data"]
        kind = d["kind"]
        if kind == "project":
            lines.append(
                "MERGE (p:Project {id:%s}) SET p.estate=true, p.name=%s, p.org=%s, "
                "p.category=%s, p.stack=%s, p.branch=%s, p.dirty=%s, p.status=%s, "
                "p.ahead=%s, p.behind=%s, p.last_commit=%s, p.age_days=%s;"
                % (esc(d["id"]), esc(d["label"]), esc(d["org"]), esc(d["category"]),
                   esc(d["stack"]), esc(d["branch"]), "true" if d["dirty"] else "false",
                   esc(d.get("status", "")), d.get("ahead", 0), d.get("behind", 0),
                   esc(d["last_commit"]), d["age_days"])
            )
        else:
            lbl = {"org": "Org", "category": "Category", "stack": "Stack"}[kind]
            lines.append("MERGE (h:%s {id:%s}) SET h.estate=true, h.name=%s, h.count=%s;"
                         % (lbl, esc(d["id"]), esc(d["label"]), d["count"]))
    lines.append("")
    rel_match = {
        "IN_ORG": ("Project", "Org"),
        "IN_CATEGORY": ("Project", "Category"),
        "USES_STACK": ("Project", "Stack"),
        "VARIANT_OF": ("Project", "Project"),
    }
    for e in model["edges"]:
        d = e["data"]
        a, b = rel_match[d["rel"]]
        lines.append(
            "MATCH (s:%s {id:%s}),(t:%s {id:%s}) MERGE (s)-[:%s]->(t);"
            % (a, esc(d["source"]), b, esc(d["target"]), d["rel"])
        )
    return "\n".join(lines) + "\n"


def validate(model: dict) -> None:
    """ponytail self-check: the one runnable thing that fails if the model breaks."""
    ids = {n["data"]["id"] for n in model["nodes"]}
    assert len(ids) == len(model["nodes"]), "duplicate node ids"
    for e in model["edges"]:
        s, t = e["data"]["source"], e["data"]["target"]
        assert s in ids, f"edge source missing node: {s}"
        assert t in ids, f"edge target missing node: {t}"
    projects = [n for n in model["nodes"] if n["data"]["kind"] == "project"]
    assert model["meta"]["total_projects"] == len(projects), "project count mismatch"
    assert model["meta"]["total_projects"] > 0, "no projects found"


def sanitize_model(model: dict) -> dict:
    """Return a public-safe copy: drop remotes/paths/branches, anonymize
    confidential-org repo names (private/local/not-git) to category
    tokens like INFRA-07, keep public/OSS repo names. Structure is preserved."""
    priv_key = CONFIG["private_org_key"]
    anon_id = CONFIG["private_org_anon_id"]
    ORG_ID = {f"org:{priv_key}": f"org:{anon_id}"}
    ORG_LBL = {priv_key: CONFIG["private_org_anon_label"]}
    id_map: dict[str, str] = {}
    new_nodes, counters = [], {}

    for n in model["nodes"]:
        d = n["data"]
        k = d["kind"]
        if k == "project":
            if d["org"] in CONFIDENTIAL_ORGS:
                code = CAT_CODE.get(d["category"], "OTH")
                counters[code] = counters.get(code, 0) + 1
                nid = f"project:anon-{code.lower()}-{counters[code]:02d}"
                new_nodes.append({"data": {
                    "id": nid, "label": f"{code}-{counters[code]:02d}", "kind": "project",
                    "org": anon_id if d["org"] == priv_key else d["org"],
                    "category": d["category"], "stack": d["stack"], "branch": "",
                    "remote": "", "dirty": d["dirty"], "is_git": d["is_git"],
                    "ahead": d.get("ahead", 0), "behind": d.get("behind", 0),
                    "status": d.get("status", "clean"),
                    "last_commit": d["last_commit"], "age_days": d["age_days"],
                    "recency": d["recency"], "path": "", "anon": True,
                }})
                id_map[d["id"]] = nid
            else:  # public / personal repo — keep name & public remote, drop local path
                nd = dict(d); nd["path"] = ""
                new_nodes.append({"data": nd}); id_map[d["id"]] = d["id"]
        elif k == "org":
            nd = dict(d); nd["id"] = ORG_ID.get(d["id"], d["id"])
            nd["label"] = ORG_LBL.get(d["label"], d["label"])
            new_nodes.append({"data": nd}); id_map[d["id"]] = nd["id"]
        else:  # category / stack hubs — generic, not sensitive
            new_nodes.append({"data": dict(d)}); id_map[d["id"]] = d["id"]

    new_edges = [{"data": {
        "id": f"e{i}", "rel": e["data"]["rel"],
        "source": id_map.get(e["data"]["source"], e["data"]["source"]),
        "target": id_map.get(e["data"]["target"], e["data"]["target"]),
    }} for i, e in enumerate(model["edges"])]

    m = dict(model["meta"])
    m["root"] = "(redacted — public build)"
    m["orgs"] = {ORG_LBL.get(k, k): v for k, v in m.get("orgs", {}).items()}
    m["org_colors"] = {anon_id: "#58a6ff"}  # drop the private-org key entirely
    m["variant_groups"] = {f"group-{i+1:02d}": ["•"] * len(v)
                           for i, v in enumerate(model["meta"].get("variant_groups", {}).values())}
    sanitized = {"meta": m, "nodes": new_nodes, "edges": new_edges}
    validate(sanitized)
    return sanitized


def build_public(model: dict) -> str:
    """Write a self-contained, sanitized site to ./docs/ — GitHub Pages serves it."""
    import shutil
    pub = os.path.join(HERE, "docs")
    os.makedirs(os.path.join(pub, "vendor"), exist_ok=True)
    safe = sanitize_model(model)
    payload = "window.ESTATE = " + json.dumps(safe, separators=(",", ":")) + ";\n"

    # index.html is the shared template; it carries no confidential literal (the
    # private-org palette key is injected from data at runtime, see meta.org_colors).
    with open(os.path.join(HERE, "index.html")) as f:
        html = f.read()

    readme = ("# Estate Graph (public site)\n\nSanitized, anonymized snapshot of a "
              "177-repo engineering estate — categories, tech stacks, git health "
              "(uncommitted / unpushed / diverged / stale) and duplicate detection. "
              "Confidential repo names, remotes and local paths are redacted; private "
              "repos appear as category tokens (e.g. `INFRA-07`). "
              "Built by `generate.py --public`; this folder is what GitHub Pages serves.\n")

    # Hard governance gate over EVERY emitted text file — not just the data.
    for fname, content in (("estate-data.js", payload), ("index.html", html),
                           ("README.md", readme)):
        for bad in FORBIDDEN_IN_PUBLIC:
            assert bad not in content, f"LEAK: '{bad}' present in public {fname} — aborting"

    with open(os.path.join(pub, "estate-data.js"), "w") as f:
        f.write(payload)
    with open(os.path.join(pub, "index.html"), "w") as f:
        f.write(html)
    shutil.copy(os.path.join(HERE, "vendor", "cytoscape.min.js"),
                os.path.join(pub, "vendor", "cytoscape.min.js"))
    open(os.path.join(pub, ".nojekyll"), "w").close()
    with open(os.path.join(pub, "README.md"), "w") as f:
        f.write(readme)
    return pub


def write_outputs(model: dict) -> None:
    with open(os.path.join(HERE, "estate.json"), "w") as f:
        json.dump(model, f, indent=2)
    with open(os.path.join(HERE, "estate-data.js"), "w") as f:
        f.write("window.ESTATE = ")
        json.dump(model, f, separators=(",", ":"))
        f.write(";\n")
    with open(os.path.join(HERE, "estate.cypher"), "w") as f:
        f.write(to_cypher(model))


def self_test() -> int:
    """Standalone security test for the sanitizer — no network, no real dirs, and
    NO confidential literal in this source. The confidential org + forbidden tokens
    are pulled from CONFIG at runtime and embedded into a synthetic confidential repo;
    the test then asserts the public build scrubs every one while preserving structure."""
    priv = CONFIG["private_org_key"]
    # Every token that must never reach a public build — embed them all into the
    # confidential repo's dropped fields so the test fails if scrubbing regresses.
    blob = " ".join(FORBIDDEN_IN_PUBLIC) or "/Users/secret"
    fixture = [
        {"name": "secret-svc", "is_git": True,
         "remote": f"https://example.com/{priv}/secret-svc.git {blob}",
         "org": priv, "branch": f"feature/{blob}", "last_epoch": NOW - DAY,
         "last_commit": "2025-01-01", "age_days": 1, "dirty": True,
         "ahead": 2, "behind": 24, "status": "diverged", "stack": "Java",
         "files_sampled": 5, "category": "Integration Infra", "stem": "secret-svc",
         "path": f"{blob}/secret-svc"},
        {"name": "my-oss-tool", "is_git": True,
         "remote": "https://github.com/ssmule/my-oss-tool.git",
         "org": "ssmule", "branch": "main", "last_epoch": NOW - DAY,
         "last_commit": "2025-01-01", "age_days": 1, "dirty": False,
         "ahead": 0, "behind": 0, "status": "clean", "stack": "Python",
         "files_sampled": 3, "category": "AI / ML", "stem": "my-oss-tool",
         "path": f"{blob}/my-oss-tool"},
    ]
    model = build_model(blob, fixture)
    validate(model)
    safe = sanitize_model(model)
    payload = json.dumps(safe)

    # 0) Sanity: the raw model really did contain the forbidden tokens (test is meaningful).
    raw = json.dumps(model)
    assert all(b in raw for b in FORBIDDEN_IN_PUBLIC), "FAIL: fixture didn't embed forbidden tokens"

    # 1) Zero leakage of any forbidden token anywhere in the public payload.
    for bad in FORBIDDEN_IN_PUBLIC + (priv,):
        assert bad not in payload, f"FAIL: '{bad}' leaked into public payload"

    # 2) Structure preserved: same node/edge counts as the real model.
    assert len(safe["nodes"]) == len(model["nodes"]), "FAIL: node count changed"
    assert len(safe["edges"]) == len(model["edges"]), "FAIL: edge count changed"

    # 3) Confidential repo anonymized to a category token; non-sensitive data kept.
    projs = [n["data"] for n in safe["nodes"] if n["data"]["kind"] == "project"]
    anon = [p for p in projs if p.get("anon")]
    assert len(anon) == 1, "FAIL: expected exactly 1 anonymized repo"
    assert re.match(r"^[A-Z]+-\d{2}$", anon[0]["label"]), "FAIL: bad anon token"
    assert anon[0]["status"] == "diverged" and anon[0]["ahead"] == 2, \
        "FAIL: git-health signal dropped during sanitize"
    # 4) Public/OSS repo name retained (not over-redacted).
    assert any(p["label"] == "my-oss-tool" for p in projs), "FAIL: public repo over-redacted"

    print("selftest OK — sanitizer scrubs confidential data, preserves structure & git-health signal")
    return 0

    # 3) Confidential repo anonymized to a category token; non-sensitive data kept.
    projs = [n["data"] for n in safe["nodes"] if n["data"]["kind"] == "project"]
    anon = [p for p in projs if p.get("anon")]
    assert len(anon) == 1, "FAIL: expected exactly 1 anonymized repo"
    assert re.match(r"^[A-Z]+-\d{2}$", anon[0]["label"]), "FAIL: bad anon token"
    assert anon[0]["status"] == "diverged" and anon[0]["ahead"] == 2, \
        "FAIL: git-health signal dropped during sanitize"
    # 4) Public/OSS repo name retained (not over-redacted).
    assert any(p["label"] == "my-oss-tool" for p in projs), "FAIL: public repo over-redacted"

    print("selftest OK — sanitizer scrubs confidential data, preserves structure & git-health signal")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return self_test()
    public = "--public" in argv
    args = [a for a in argv[1:] if a not in ("--selfcheck", "--public", "--selftest")]
    root = args[0] if args else DEFAULT_ROOT
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    model = scan(root)
    validate(model)
    write_outputs(model)
    m = model["meta"]
    print(f"Scanned {m['root']}")
    print(f"  projects={m['total_projects']}  git={m['git_repos']}  non-git={m['non_git']}"
          f"  dirty={m['dirty']}  unpushed={m['unpushed']}  diverged={m['diverged']}"
          f"  stale>90d={m['stale_90d']}")
    print(f"  needs-attention={m['attention']}")
    print(f"  nodes={len(model['nodes'])}  edges={len(model['edges'])}"
          f"  categories={len(m['categories'])}  stacks={len(m['stacks'])}")
    print(f"  variant groups (possible dupes)={len(m['variant_groups'])}")
    print("Wrote estate.json, estate-data.js, estate.cypher")
    if public:
        pub = build_public(model)
        print(f"Public build (sanitized) -> {pub}")
    print("Open index.html in a browser for the interactive graph.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
