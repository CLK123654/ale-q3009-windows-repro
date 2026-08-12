from __future__ import annotations

import argparse
import atexit
import csv
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REQUIRED = {
    "README.md",
    "tenants.csv",
    "experiment_allocations.csv",
    "consent_snapshots.csv",
    "exposure_events.csv",
    "rules/attribution_rules.md",
    "handoff_request.json",
}
CSV_KEYS = {
    "tenants.csv": "tenant_id",
    "experiment_allocations.csv": "allocation_id",
    "exposure_events.csv": "event_id",
}


def run(command: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, input=stdin, text=True, capture_output=True, timeout=300)
    if completed.returncode:
        raise RuntimeError(completed.stdout + completed.stderr)
    return completed


def psql(binary: str, url: str) -> list[str]:
    return [binary, "--dbname", url, "-X", "--quiet", "--set", "ON_ERROR_STOP=1"]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def export(binary: str, url: str, query: str, path: Path) -> None:
    command = psql(binary, url) + ["--command", f"COPY ({query}) TO STDOUT WITH(FORMAT CSV,HEADER TRUE)"]
    path.write_text(run(command).stdout, encoding="utf-8", newline="")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--psql", required=True)
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    input_root = Path(args.input).resolve()
    output = Path(args.output).resolve()
    if output.exists():
        shutil.rmtree(output)
    complete = {"value": False}

    def cleanup() -> None:
        if not complete["value"] and output.exists():
            shutil.rmtree(output)

    atexit.register(cleanup)
    present = {p.relative_to(input_root).as_posix() for p in input_root.rglob("*") if p.is_file()}
    if present != REQUIRED:
        raise ValueError("月结材料集合发生变化")
    for filename, key in CSV_KEYS.items():
        rows = read_csv(input_root / filename)
        values = [row.get(key, "") for row in rows]
        if not values or any(not value for value in values) or any(count > 1 for count in Counter(values).values()):
            raise ValueError(f"{filename}业务键缺失或重复")
    handoff = json.loads((input_root / "handoff_request.json").read_text(encoding="utf-8"))
    if set(handoff.get("required_roles", [])) != {"tenant_video_ro", "tenant_shop_ro", "platform_auditor"}:
        raise ValueError("月结访问角色不完整")

    output.mkdir(parents=True)
    (output / "sql").mkdir()
    (output / "results").mkdir()
    for filename in ["schema.sql", "attribution.sql", "rls.sql"]:
        shutil.copy2(ROOT / filename, output / "sql" / filename)
    statements = [
        "DROP SCHEMA IF EXISTS cdp CASCADE;",
        (ROOT / "schema.sql").read_text(encoding="utf-8"),
    ]
    load_map = [
        ("tenants.csv", "cdp.tenant_account", None),
        ("experiment_allocations.csv", "cdp.experiment_allocation", "allocation_id,tenant_id,experiment_path,variant,starts_at_utc,ends_at_utc,bucket_start,bucket_end,priority,enabled"),
        ("consent_snapshots.csv", "cdp.consent_snapshot", "tenant_id,user_id_hash,consent_start_utc,consent_end_utc,allow_experiment"),
        ("exposure_events.csv", "cdp.exposure_event_raw", None),
    ]
    run(psql(args.psql, args.database_url), "BEGIN;\n" + "\n".join(statements) + "\nCOMMIT;\n")
    for filename, table, columns in load_map:
        column_clause = f" ({columns})" if columns else ""
        sql = f"COPY {table}{column_clause} FROM STDIN WITH(FORMAT CSV,HEADER TRUE);"
        run(psql(args.psql, args.database_url) + ["--command", sql], (input_root / filename).read_text(encoding="utf-8-sig"))
    run(
        psql(args.psql, args.database_url),
        "BEGIN;\n"
        + (ROOT / "attribution.sql").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "rls.sql").read_text(encoding="utf-8")
        + "\nCOMMIT;\n",
    )

    results = output / "results"
    export(args.psql, args.database_url, "SELECT event_id,tenant_id,user_id_hash,allocation_id,experiment_path,variant,to_char(matched_at_utc AT TIME ZONE 'UTC','YYYY-MM-DD\\\"T\\\"HH24:MI:SS\\\"Z\\\"') AS matched_at_utc,bucket,match_reason FROM cdp.exposure_attribution ORDER BY event_id", results / "attributed_exposure.csv")
    export(args.psql, args.database_url, "SELECT event_id,tenant_id,reject_reason,detail FROM cdp.rejected_exposure ORDER BY event_id", results / "rejected_exposure.csv")
    export(args.psql, args.database_url, "SELECT tenant_id,experiment_path,variant,exposure_count,distinct_users FROM cdp.path_variant_rollup ORDER BY tenant_id,experiment_path,variant", results / "path_variant_rollup.csv")

    access_rows: list[dict[str, str]] = []
    for role, setting in [("tenant_video_ro", "video"), ("tenant_shop_ro", "shop"), ("platform_auditor", "")]:
        setting_sql = f"SET app.tenant_id={literal(setting)};" if setting else "RESET app.tenant_id;"
        query = (
            f"SET SESSION AUTHORIZATION {role};{setting_sql}"
            "SELECT session_user,current_user,COALESCE(current_setting('app.tenant_id',true),''),"
            "count(*),COALESCE(string_agg(DISTINCT tenant_id,'|' ORDER BY tenant_id),'') "
            "FROM cdp.exposure_attribution;"
        )
        raw = run(psql(args.psql, args.database_url) + ["--tuples-only", "--no-align", "--field-separator", "\t", "--command", query]).stdout.strip().splitlines()[-1]
        session_user, current_user, tenant_setting, visible_rows, visible_tenants = raw.split("\t")
        access_rows.append({"role_name": role, "session_user": session_user, "current_user": current_user, "tenant_setting": tenant_setting, "visible_rows": visible_rows, "visible_tenants": visible_tenants})
    with (results / "tenant_access_summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(access_rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(access_rows)

    summary = {
        "statement_period": handoff["statement_period"],
        "cutoff_at_utc": handoff["cutoff_at_utc"],
        "attribution_owner": handoff["attribution_owner"],
        "access_review_owner": handoff["access_review_owner"],
        "downstream_consumer": handoff["downstream_consumer"],
        "attributed_rows": sum(1 for _ in read_csv(results / "attributed_exposure.csv")),
        "rejected_rows": sum(1 for _ in read_csv(results / "rejected_exposure.csv")),
        "rollup_rows": sum(1 for _ in read_csv(results / "path_variant_rollup.csv")),
    }
    (output / "review-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "README.md").write_text(
        "这份月结材料交给实验运营组和数据安全组。results/attributed_exposure.csv是曝光归因明细，results/rejected_exposure.csv记录未归因原因，results/path_variant_rollup.csv用于当月实验汇总。\n\nresults/tenant_access_summary.csv记录三个数据库身份实际读取到的租户范围。sql目录保存归因表结构、归因查询和租户访问策略，review-summary.json记录本次月结范围与接收人。\n",
        encoding="utf-8",
    )
    complete["value"] = True


if __name__ == "__main__":
    main()
