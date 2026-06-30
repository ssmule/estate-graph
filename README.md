# Estate Graph

An interactive, **offline**, Neo4j-style graph of the *current state* of every
project under `~/Work/projects` — your whole working estate as one view: git
health (uncommitted, **unpushed, diverged**, stale), tech stack, owning org,
category, and duplicate/variant repos.

![preview](preview.png)

## Open it

```bash
open index.html        # double-click works too — no server, no internet needed
```

Vendored Cytoscape.js renders it locally (works behind the Zscaler proxy).

## Regenerate (re-scan current state)

```bash
python3 generate.py            # scans ~/Work/projects (the estate) by default
python3 generate.py /some/dir  # scan a different root
```

Stdlib only, no dependencies, deterministic, no network calls. Re-run whenever
the estate changes; refresh the browser. The generator self-checks its model
(every edge endpoint must resolve) before writing — it fails loudly if not.

```bash
python3 generate.py --selftest   # security test: proves --public scrubs all confidential data
```

## What you're looking at

| Element | Meaning |
|---|---|
| **Big dots** | repositories / project dirs. Size = recency (recent commit → bigger). |
| **Repo ring colour** | git health — <span>orange</span> = diverged (ahead **and** behind upstream), red = uncommitted, amber = unpushed (local commits not pushed), grey = stale (>90d). |
| **Ringed labels** | hub nodes — Category (green), Org (blue), Stack (purple diamond). |
| **Orange dashed arrow** | `VARIANT_OF` — a likely duplicate (`foo` ↔ `foo-main`, `foo` ↔ `foo-old`, `x.repos`). |

**Git health is computed fully offline** (no fetch): ahead/behind come from each
repo's recorded upstream (`@{upstream}`), so "unpushed work at risk" is surfaced
without touching the network.

Header KPIs summarise the estate (repos, git, uncommitted, **unpushed,
diverged**, stale, **attention**, stacks). Controls: search, recolour by
**Category / Org / Stack**, toggle which link types are drawn, switch layout,
filter by category, **Show all labels**, **⚠ Needs attention** (spotlights every
repo needing action), and **⬇ Export PNG** (portfolio-ready snapshot). Click any
node for full detail — status, ahead/behind, remote, branch, last commit, path.

## Files

| File | Role | Tracked in git? |
|---|---|---|
| `generate.py` | scanner + model builder + exporters + sanitizer | ✅ yes (source) |
| `index.html` | the interactive view (static template) | ✅ yes (source) |
| `vendor/cytoscape.min.js` | graph engine, vendored for offline use | ✅ yes |
| `docs/` | sanitized site GitHub Pages serves (`--public` output) | ✅ yes |
| `estate.config.json` | **confidential** org/identifier patterns | 🚫 git-ignored |
| `estate-data.js` `estate.json` `estate.cypher` | generated model (real names/paths) | 🚫 git-ignored |
| `preview.png` | snapshot for this README | 🚫 git-ignored |

> **One repo, clean split:** the *source* is version-controlled and public; the
> *real scan outputs* and the *confidential config* never leave your machine
> (git-ignored). The only published data is the sanitized `docs/` site.

## Publish to GitHub Pages (sanitized)

**Live:** https://ssmule.github.io/estate-graph/ · repo `ssmule/estate-graph`,
Pages source = `main` branch, `/docs` folder.

The raw outputs embed confidential data (private remote URLs, local paths) —
they're git-ignored and never published. The site is a **sanitized** build:

```bash
python3 generate.py --public      # writes ./docs/ (anonymized, no remotes/paths)
```

`--public` drops `remote`/`path`/`branch`, anonymizes private/local/not-git repo
*names* to category tokens (`INFRA-07`, `MULE-03`), keeps your own public/OSS repo
names, and **hard-fails** if any forbidden string (defined in `estate.config.json`)
survives in *any* emitted file. Confidential identifiers live only in the
untracked `estate.config.json`, so this source is safe to publish. To refresh the
live site:

```bash
python3 generate.py --public && git add docs && git commit -m "refresh site" && git push
```

GitHub Pages rebuilds `/docs` automatically on push.

### `estate.config.json` (untracked — create once)

```json
{
  "private_org_match": "<substring in your private remotes, lowercased>",
  "private_org_key": "<Org name as shown locally>",
  "private_org_anon_id": "enterprise",
  "private_org_anon_label": "Enterprise (private)",
  "forbidden_in_public": ["<Org name>", "/Users/", "<your username>"]
}
```

Absent this file, the tool runs with generic, literal-free defaults (no private
org; only `/Users/` is treated as forbidden).

## Load into Neo4j (optional — you said "like neo4j")

```bash
cat estate.cypher | cypher-shell -u neo4j -p <password>
# then in Neo4j Browser:
MATCH (p:Project)-[:IN_CATEGORY]->(c:Category) RETURN p,c;          # the estate
MATCH (p:Project {dirty:true}) RETURN p.name, p.category;           # uncommitted work
MATCH (p:Project) WHERE p.ahead > 0 RETURN p.name, p.ahead, p.behind, p.status; # unpushed / at risk
MATCH (a:Project)-[:VARIANT_OF]->(b:Project) RETURN a.name, b.name; # duplicates to prune
MATCH (p:Project) WHERE p.age_days > 90 RETURN p.name, p.last_commit ORDER BY p.age_days DESC;
```

Reset: `MATCH (n) WHERE n.estate = true DETACH DELETE n;`

## Model

```
(:Project)-[:IN_ORG]->(:Org)
(:Project)-[:IN_CATEGORY]->(:Category)
(:Project)-[:USES_STACK]->(:Stack)
(:Project)-[:VARIANT_OF]->(:Project)
```

## Heuristics (tune in `generate.py`)

- **Category** — ordered keyword rules (`CATEGORIES`); first match wins. Wrong
  bucket? add/adjust one rule.
- **Stack** — file-extension histogram (walk depth ≤ 6) plus build-marker boosts
  (`pom.xml`/`build.gradle` → Java, `mule-artifact.json` → Mule, etc.) so a
  Micronaut service isn't mislabelled by shallow scripts.
- **Variant** — names normalised by stripping `-main`/`-old`/`-backup`/`.repos`/
  trailing version digits; shared stems get linked.
- **Git health (`status`)** — single attention-priority label per repo, computed
  offline: `diverged` (ahead & behind) → `dirty` (uncommitted) → `unpushed`
  (ahead only) → `stale` (>90d) → `clean`. Drives the ring colour and the
  **Needs attention** filter.
