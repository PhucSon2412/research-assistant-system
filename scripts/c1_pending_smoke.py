"""C1 API/web/worker/beat smoke in an existing, external disposable Compose context.

Uses no .env, dev project, provider key or production delivery. Never opens safety.
"""
import argparse
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main():
    p=argparse.ArgumentParser();p.add_argument("--context",type=Path,required=True);a=p.parse_args()
    if Path(sys.executable).resolve()!=Path(r"C:\Users\ADMIN\.conda\envs\ras\python.exe").resolve():
        raise SystemExit("use Conda ras interpreter")
    repo=Path(__file__).resolve().parents[1];c=json.loads(a.context.read_text(encoding="utf-8"));root=Path(c["dir"]).resolve()
    if repo==root or repo in root.parents or not c["project"].startswith("rasc1"):
        raise SystemExit("external isolated context required")
    compose=root/"compose.json";data=json.loads(compose.read_text(encoding="utf-8"))
    cmd=["docker","compose","--env-file",str(root/"empty.env"),"-p",c["project"],"-f",str(compose)]
    def run(args,**kw):return subprocess.run(cmd+args,check=True,**kw)
    def port(service,internal):return int(subprocess.check_output(cmd+["port",service,str(internal)],text=True).strip().rsplit(":",1)[1])
    minio=data["services"]["minio"]["environment"]
    redis_password=c.setdefault("redis_password",secrets.token_urlsafe(24));a.context.write_text(json.dumps(c),encoding="utf-8")
    journal=root/"smoke-journal";journal.mkdir(exist_ok=True)
    base_env={"RAS_DB__HOST":"db","RAS_DB__PORT":"5432","RAS_DB__USER":c["runtime"],"RAS_DB__PASSWORD":c["runtime_password"],"RAS_DB__NAME":"c1_control",
        "RAS_RECOVERY__JOURNAL_PATH":"/var/lib/ras/journal/revocations.jsonl","RAS_REDIS_HOST":"redis","RAS_REDIS_PASSWORD":redis_password,
        "RAS_STORAGE__ENDPOINT_URL":"http://minio:9000","RAS_STORAGE__ACCESS_KEY":minio["MINIO_ROOT_USER"],"RAS_STORAGE__SECRET_KEY":minio["MINIO_ROOT_PASSWORD"],
        "RAS_INGEST__POLL_SECONDS":"2","RAS_RETENTION__CLEANUP_POLL_SECONDS":"2"}
    api={"build":{"context":str(repo/"api")},"image":c["project"]+"-api","environment":base_env,"volumes":[str(journal)+":/var/lib/ras/journal"]}
    data["services"].update({
        "redis":{"image":"redis:7.4-alpine","command":["redis-server","--requirepass",redis_password]},
        "api":api|{"ports":["127.0.0.1::8000"]},
        "web":{"build":{"context":str(repo/"web")},"environment":{"API_INTERNAL_URL":"http://api:8000"},"ports":["127.0.0.1::3000"]},
        "worker":api|{"command":["celery","-A","app.ingest.celery_app:celery_app","worker","--pool=solo","--concurrency=1","--loglevel=INFO"]},
        "beat":api|{"command":["celery","-A","app.ingest.celery_app:celery_app","beat","--schedule=/tmp/c1-beat","--loglevel=INFO"]},
    })
    compose.write_text(json.dumps(data),encoding="utf-8")
    run(["stop","api","web","worker","beat"])
    env={k:v for k,v in os.environ.items() if not k.startswith("RAS_")}
    env.update(base_env);env.update({"RAS_DB__HOST":"127.0.0.1","RAS_DB__PORT":str(port("db",5432)),"RAS_DB__USER":c["dbuser"],"RAS_DB__PASSWORD":c["password"],
        "RAS_RECOVERY__JOURNAL_PATH":str(journal/"revocations.jsonl"),"RAS_STORAGE__ENDPOINT_URL":"http://127.0.0.1:"+str(port("minio",9000))})
    def host(args):subprocess.run([sys.executable,*args],cwd=repo/"api",env=env,check=True)
    host(["-m","alembic","upgrade","head"])
    host(["-m","app.cli.recovery","init","--if-new"])
    host(["-m","app.cli.provision_auth_roles","--runtime",c["runtime"],"--recovery",c["recovery"],"--definer","c1_definer"])
    env.update({"RAS_DB__USER":c["recovery"],"RAS_DB__PASSWORD":c["recovery_password"]})
    host(["-m","app.cli.recovery","recover"])
    run(["build","api","web"])
    run(["up","-d","redis","api","web","worker","beat"])
    apibase="http://127.0.0.1:"+str(port("api",8000));webbase="http://127.0.0.1:"+str(port("web",3000))
    def fetch(base,path,method="GET"):
        request=Request(base+path,data=b"{}" if method=="POST" else None,method=method,headers={"Content-Type":"application/json"})
        try:
            with urlopen(request,timeout=5) as response:return response.status,response.read()
        except HTTPError as exc:return exc.code,exc.read()
    deadline=time.monotonic()+55
    while True:
        try:
            if fetch(apibase,"/healthz")[0]==200 and fetch(webbase,"/login")[0]==200:break
        except (URLError,TimeoutError,ConnectionError):pass
        if time.monotonic()>deadline:raise RuntimeError("isolated services failed startup")
        time.sleep(1)
    checks=[(apibase,"/healthz","GET",200),(apibase,"/readyz","GET",503)]
    for base,prefix in ((apibase,""),(webbase,"/api")):
        for path,method in (("/auth/login","POST"),("/auth/csrf","GET"),("/library/items","GET"),("/admin/users","GET"),("/search","POST"),("/rag/ask","POST"),
            ("/files/00000000-0000-0000-0000-000000000000/original","GET")):
            checks.append((base,prefix+path,method,503))
    for base,path,method,expected in checks:
        code,body=fetch(base,path,method)
        assert code==expected,(path,code,expected)
        if expected==503 and path!="/readyz":assert json.loads(body)=={"error":"service_recovering"}
        print("pending HTTP:","web" if base==webbase else "api",method,path,code,flush=True)
    run(["exec","-T","worker","python","-c","from app.ingest.celery_app import celery_app; [celery_app.send_task(n) for n in ('ras.ingest.discover','ras.discovery.sync','ras.citations.reconcile','ras.revocation.cleanup')]"])
    # process needs an ID; send that task separately with valid args.
    run(["exec","-T","worker","python","-c","from app.ingest.celery_app import celery_app; celery_app.send_task('ras.ingest.process',args=['00000000-0000-0000-0000-000000000000'])"])
    time.sleep(5)
    logs=subprocess.check_output(cmd+["logs","--no-color","worker","beat"],text=True,encoding="utf-8",errors="replace")
    assert "auth_safety_pending" in logs
    for name in ("ras.ingest.discover","ras.discovery.sync","ras.citations.reconcile","ras.revocation.cleanup","ras.ingest.process"):
        assert re.search(r"Task "+re.escape(name)+r"\[.*?\] succeeded .*?: (0|.*recovery_gate_closed.*)",logs),name
    assert "Sending due task discover-ingest-jobs" in logs and "Sending due task revoked-copy-cleanup" in logs
    (root/"smoke-worker.log").write_text(logs,encoding="utf-8")
    print("worker/beat observed pending; pricing_verified unchanged; safety remains pending",flush=True)


if __name__=="__main__":main()
