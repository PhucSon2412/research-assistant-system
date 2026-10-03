"""Sequential C1 guard counterchecks; always restore bytes before another run."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys


def main():
    p=argparse.ArgumentParser();p.add_argument("--context",required=True);args=p.parse_args()
    repo=Path(__file__).resolve().parents[1]
    if Path(sys.executable).resolve()!=Path(r"C:\Users\ADMIN\.conda\envs\ras\python.exe").resolve():
        raise SystemExit("use Conda ras interpreter")
    binding="""       OR ROW(NEW.auth_user_id,NEW.auth_operation_id,NEW.auth_generation,NEW.auth_domain,NEW.auth_decision,NEW.auth_reason_code,NEW.auth_recorded_at)
          IS DISTINCT FROM ROW((b->>'user_id')::uuid,(b->>'operation_id')::uuid,(b->>'generation')::uuid,domain,decision,b->>'reason_code',stamp)
"""
    cases=[
        ("typed-null-binding","api/migrations/versions/0033_auth_safety_foundation.sql",binding,"", "typed_mirror_binding and True and auth_user_id"),
        ("runtime-raw-write","api/app/db_roles.py","if table not in PROTECTED and table !=", "if (table not in PROTECTED or table == 'users') and table !=", "runtime_raw_mutation and active"),
        ("uuid-history","api/app/auth_safety.py","seen = set(reserved)\n    for r in records:", "seen = set(reserved)\n    for r in ():", "absent_user_projection_replay_and_uuid_reuse"),
    ]
    runner=repo/"scripts/c1_test_host.py"
    for label,relative,old,new,selector in cases:
        path=repo/relative;original=path.read_bytes();before=hashlib.sha256(original).hexdigest()
        source=original.decode("utf-8").replace("\r\n","\n")
        if source.count(old)!=1:raise SystemExit("mutation anchor mismatch: "+label)
        try:
            path.write_bytes(source.replace(old,new).encode("utf-8"))
            code=subprocess.call([sys.executable,str(runner),"--context",args.context,"api/tests/test_auth_safety_foundation.py","-q","-k",selector,"--tb=short"],cwd=repo)
            if code!=1:raise SystemExit("countercheck must fail exactly with test failure: "+label)
            print("countercheck detected:",label,flush=True)
        finally:
            path.write_bytes(original)
            if hashlib.sha256(path.read_bytes()).hexdigest()!=before:raise SystemExit("restoration failed")
        print("original bytes restored:",relative,flush=True)
    return 0


if __name__=="__main__":sys.exit(main())
