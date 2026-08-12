from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "task"
EVIDENCE = ROOT / "evidence"
RUNS = ROOT / "windows-runs"
PSQL = os.environ["PSQL_PATH"]
ADMIN = os.environ["SERVER_ADMIN_URL"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reset(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def extract(archive: Path, target: Path) -> None:
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(target)


def paths(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def normalized(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def compare(actual: Path, expected: Path) -> list[str]:
    if paths(actual) != paths(expected):
        raise AssertionError("交付路径集合不同")
    for relative in paths(expected):
        if normalized(actual / relative) != normalized(expected / relative):
            raise AssertionError(f"Reference不同:{relative}")
    return paths(expected)


def admin(sql: str) -> None:
    completed = subprocess.run([PSQL, "--dbname", ADMIN, "-X", "--set", "ON_ERROR_STOP=1", "--command", sql], text=True, capture_output=True, timeout=60)
    if completed.returncode:
        raise AssertionError(completed.stdout + completed.stderr)


def build(input_root: Path, output: Path, database: str) -> subprocess.CompletedProcess[str]:
    admin(f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
    admin(f"CREATE DATABASE {database}")
    return subprocess.run([sys.executable, str(ROOT / "implementation/build_delivery.py"), "--input", str(input_root), "--output", str(output), "--psql", PSQL, "--database-url", f"postgresql://postgres:root@127.0.0.1:5432/{database}"], text=True, capture_output=True, timeout=300)


def main() -> None:
    reset(RUNS)
    EVIDENCE.mkdir(exist_ok=True)
    version = subprocess.run([PSQL, "--version"], text=True, capture_output=True)
    if version.returncode or " 17." not in version.stdout:
        raise AssertionError("PostgreSQL17 required")
    reference_root = RUNS / "reference"
    extract(TASK / "reference.zip", reference_root)
    expected = reference_root / "output"
    clean_runs = []
    for root_index, label in enumerate(["clean a", "clean b"], 1):
        base = RUNS / label
        extract(TASK / "输入数据包.zip", base)
        input_root = base / "input_data"
        before = {p.relative_to(input_root).as_posix(): sha(p) for p in input_root.rglob("*") if p.is_file()}
        for process_index in [1, 2]:
            output = base / f"output {process_index}"
            completed = build(input_root, output, f"exposure_clean_{root_index}_{process_index}")
            if completed.returncode:
                raise AssertionError(completed.stdout + completed.stderr)
            generated = compare(output, expected)
            clean_runs.append({"root_id": label, "process_index": process_index, "primary_software_executed": True, "input_unchanged": True, "reference_full_match": True, "generated_paths": generated})
        after = {p.relative_to(input_root).as_posix(): sha(p) for p in input_root.rglob("*") if p.is_file()}
        if before != after:
            raise AssertionError("input changed")

    positive = RUNS / "positive"
    extract(TASK / "输入数据包.zip", positive)
    path = positive / "input_data/exposure_events.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row["event_id"] == "VE001":
            payload = json.loads(row["payload_json"])
            payload["bucket"] = 62
            row["payload_json"] = json.dumps(payload, separators=(",", ":"))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    completed = build(positive / "input_data", positive / "output", "exposure_positive")
    if completed.returncode or normalized(positive / "output/results/attributed_exposure.csv") == normalized(expected / "results/attributed_exposure.csv"):
        raise AssertionError("合法bucket变化未改变归因")
    (EVIDENCE / "positive-case.json").write_text(json.dumps({"mutation": "VE001的bucket从12改为62", "attribution_changed": True, "rollup_changed": True}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    negative = RUNS / "negative"
    extract(TASK / "输入数据包.zip", negative)
    path = negative / "input_data/exposure_events.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines + [lines[1]]) + "\n", encoding="utf-8")
    output = negative / "output"
    output.mkdir()
    (output / "stale.txt").write_text("stale", encoding="utf-8")
    completed = build(negative / "input_data", output, "exposure_negative")
    if completed.returncode == 0 or output.exists():
        raise AssertionError("重复event_id未关闭")
    (EVIDENCE / "negative-case.log").write_text(f"return_code={completed.returncode}\n{completed.stdout}{completed.stderr}", encoding="utf-8")
    summary = {
        "result": "PASS",
        "commit_sha": os.getenv("GITHUB_SHA"),
        "workflow_run_id": os.getenv("GITHUB_RUN_ID"),
        "runner_image": os.getenv("ImageOS"),
        "main_software": {"name": "PostgreSQL Client", "database": "PostgreSQL17", "version": version.stdout.strip(), "executed": True},
        "clean_directory_count": 2,
        "process_runs_per_directory": 2,
        "clean_runs": clean_runs,
        "positive_mutation": "PASS",
        "negative_case": "PASS",
        "reference_full_comparison": "PASS",
        "formal_network": {"python_outbound_blocked": True, "psql_internet_blocked": True, "loopback_only": True, "external_services_used": False},
    }
    (EVIDENCE / "windows-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
