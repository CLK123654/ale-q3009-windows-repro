from __future__ import annotations
import json,os,shutil,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];WORK=ROOT/"work-reference";EVIDENCE=ROOT/"evidence"
if WORK.exists():shutil.rmtree(WORK)
WORK.mkdir()
with zipfile.ZipFile(ROOT/"task/输入数据包.zip") as z:z.extractall(WORK)
completed=subprocess.run([sys.executable,str(ROOT/"implementation/build_delivery.py"),"--input",str(WORK/"input_data"),"--output",str(WORK/"output"),"--psql",os.environ["PSQL_PATH"],"--database-url",os.environ["REFERENCE_DATABASE_URL"]],text=True,capture_output=True,timeout=300)
if completed.returncode:raise SystemExit(completed.stdout+completed.stderr)
EVIDENCE.mkdir(exist_ok=True)
with zipfile.ZipFile(EVIDENCE/"reference-candidate.zip","w",compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
    for p in sorted((WORK/"output").rglob("*")):
        if p.is_file():z.write(p,p.relative_to(WORK).as_posix())
(EVIDENCE/"reference-generation.json").write_text(json.dumps({"result":"PASS","commit_sha":os.getenv("GITHUB_SHA"),"workflow_run_id":os.getenv("GITHUB_RUN_ID")},indent=2)+"\n")
