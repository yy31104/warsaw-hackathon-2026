"""CLI for frozen synthetic M3 measurements. Run from the repository root."""
import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from alerting import llm

from .ledger import Journal, Rate, usd
from .report import choose, summarize, write_report
from .runner import HERE, OFFLINE, OUTPUT_CAP, RATE_CARD, discover, evaluate, not_run


ROOT = HERE.parent.parent
SPLITS = {"dev": (8, 7), "heldback": (4, 3)}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args], text=True).strip()


def journal_path():
    if configured := os.getenv("W3_EVAL_JOURNAL"):
        path = Path(configured)
        if not path.is_absolute():
            raise ValueError("W3_EVAL_JOURNAL must be an absolute path shared by the campaign")
        return path
    common = Path(git(ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return common.parent / "loadtest/results/tmp/m3-budget.sqlite3"


def source_hash():
    # The production code may be uncommitted during development. Hash relevant source bytes
    # as well as recording its Git revision for the evidence.
    base = Path(llm.__file__).resolve().parent
    files = [*base.glob("assist/*.py"), *base.glob("runbooks/actions/*.py"),
             *(base / f for f in ("llm.py", "ingest/normalize.py", "schemas/mapping.py",
                                   "contracts.py", "templates.py", "rules/dsl.py"))]
    return digest(json.dumps({str(p.relative_to(base)): digest(p.read_bytes()) for p in sorted(files)}).encode())


def read_cases(split):
    folder = HERE / "cases"
    frozen = {name: checksum for checksum, name in (line.split() for line in (folder / "SHA256SUMS").read_text().splitlines())}
    tasks, hashes, ids = [], {}, set()
    for task, n in zip(("schema", "runbook"), SPLITS[split]):
        name = f"{split}_{task}s.json"
        data = (folder / name).read_bytes()
        hashes[name] = digest(data)
        if hashes[name] != frozen[name]:
            raise ValueError(f"fixture differs from frozen manifest: {name}")
        cases = json.loads(data)
        if len(cases) != n:
            raise ValueError(f"wrong case count: {name}")
        for c in cases:
            if c["id"] in ids:
                raise ValueError("duplicate fixture ID")
            ids.add(c["id"])
            tasks.append((task, c))
    return tasks, hashes


def prior(journal, phase, signature):
    if phase in ("preview", "baseline"):
        return None
    previous = "tuned" if phase == "final" else "baseline"
    manifest, report = journal.phase(previous)
    if report is None:
        raise ValueError(f"finish {previous} before {phase}")
    check = ["families", "models", "output_cap", "rates_sha256", "fixtures_manifest_sha256", "scorer_sha256"]
    if phase == "final":
        check.append("production_sha256")
    if any(manifest[k] != signature[k] for k in check):
        raise ValueError(f"{phase} settings differ from the frozen {previous} campaign")
    return report


async def campaign(args, journal):
    phase = args.phase
    families = list(RATE_CARD)
    old_phase = {"tuned": "baseline", "final": "tuned"}.get(phase, phase)
    old, _ = journal.phase(old_phase)
    inventory, issue = ({}, "offline mode") if args.offline or phase == "preview" else await discover(families)
    if not issue and not all(hasattr(llm, k) for k in ("observe_calls", "count_openai_input")):
        issue = "production M2 observation adapter unavailable"
    models = old["models"] if old else {f: inventory.get(f, {}).get("selected") or f for f in families}
    signature = {
        "families": families, "models": models, "output_cap": OUTPUT_CAP,
        "production_sha256": source_hash(), "rates_sha256": digest((HERE / "rates.json").read_bytes()),
        "fixtures_manifest_sha256": digest((HERE / "cases/SHA256SUMS").read_bytes()),
        "scorer_sha256": digest((HERE / "scoring.py").read_bytes()),
    }
    # This check happens BEFORE reading any final fixture. Only published synthetic file
    # names can be loaded, and only the final phase may read the held-back pair.
    earlier = prior(journal, phase, signature)
    split = "heldback" if phase == "final" else "dev"
    cases, hashes = read_cases(split)
    signature.update(split=split, fixtures_sha256=hashes)
    journal.start(phase, signature)
    _, completed = journal.phase(phase)
    modes = [OFFLINE, *models.values()]
    rows, aliases = [], {}
    for mode in modes:
        family = next((f for f in families if models[f] == mode), None)
        rate = Rate.from_dict(RATE_CARD[family]) if family else None
        entry = inventory.get(family, {})
        listed = entry.get("listed_family") if mode == family else mode in entry.get("listed_snapshots", [])
        for task, case in cases:
            claim, row = journal.claim(phase, mode, case["id"])
            if not claim and row is None:
                row = not_run(mode, task, case["id"], "interrupted case; never reissued")
                row["call"] = journal.receipt(phase, mode, case["id"])
            if row is None and family and (issue or not listed):
                row = not_run(mode, task, case["id"], issue or "requested model absent from SDK inventory")
            if row is None:
                row = await evaluate(phase, mode, task, case, journal, rate, aliases.get(case["id"]))
            if mode == OFFLINE and task == "schema" and "actual" in row:
                aliases[case["id"]] = row["actual"]["aliases"]
            row["expected"] = {k: v for k, v in case.items() if k not in ("id", "sample", "text", "format")}
            journal.save(phase, mode, case["id"], row)
            rows.append(row)

    if completed:
        manifest = completed["manifest"]
        summary = completed["summary"]
    else:
        stats = {m: summarize([r for r in rows if r["mode"] == m]) for m in modes}
        selection = earlier["summary"]["selection"] if phase == "final" else choose(stats, list(models.values()))
        summary = dict(phase=phase, split=split, n_cases=len(cases), modes=stats, selection=selection,
                       campaign_exposure_usd=usd(journal.exposure()))
        manifest = {**signature, "phase": phase, "source_revision": git(Path(llm.__file__).parent, "rev-parse", "HEAD"),
                    "runner_revision": git(ROOT, "rev-parse", "HEAD"), "pricing": RATE_CARD,
                    "inventory": inventory, "inventory_error": issue, "budget_usd": "5"}
        journal.finish(phase, {"manifest": manifest, "summary": summary})
    directory = args.output / phase
    write_report(directory, phase, manifest, rows, summary)
    print((directory / "summary.md").read_text())
    return summary


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("discover", "preview", "baseline", "tuned", "final", "budget"))
    parser.add_argument("--offline", action="store_true", help="run deterministic code and mark both provider modes NOT_RUN")
    parser.add_argument("--output", type=Path, default=ROOT / "loadtest/results/llm-eval",
                        help="evidence directory; each phase has its own immutable subdirectory")
    args = parser.parse_args()
    if args.phase == "discover":
        candidates, issue = await discover(list(RATE_CARD))
        print(json.dumps({"status": "NOT_RUN" if issue else "PASS", "candidates": candidates, "reason": issue}, indent=2))
        return
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "preview.sqlite3" if args.phase == "preview" else journal_path()
        journal = Journal(path)
        if args.phase == "budget":
            print(json.dumps({"journal": str(path), "limit_usd": "5", "accounted_usd": usd(journal.exposure())}))
            return
        with journal.campaign_lock():
            await campaign(args, journal)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (ValueError, OSError, KeyError) as e:
        print(f"Evaluation stopped: {e}", file=sys.stderr)
        sys.exit(2)
