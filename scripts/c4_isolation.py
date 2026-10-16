"""Expose fixed test targets without giving application containers outbound routes."""
import json
from pathlib import Path
import secrets
import socket

_allocated=set()


def free_port():
    # Outside Windows' usual ephemeral client range: SQL clients running in
    # parallel must not consume a probed server port before Docker binds it.
    for _ in range(128):
        candidate=20000+secrets.randbelow(25000)
        if candidate in _allocated:continue
        with socket.socket() as probe:
            try:probe.bind(('127.0.0.1',candidate))
            except OSError:continue
        _allocated.add(candidate)
        return candidate
    raise RuntimeError('isolated listener port unavailable')


def write_ports_override(root):
    path=root/'ports.yaml'
    path.write_text('services:\n'+''.join(f'  {name}:\n    ports: !reset []\n' for name in ('api','web','db','minio')),encoding='utf-8')
    return path

RELAY = """const net=require('net');
for (const [port,host,target] of [[8000,'api',8000],[3000,'web',3000],[5432,'db',5432],[9000,'minio',9000],[9001,'minio',9001]]) {
 net.createServer(s=>{ s.setNoDelay(true); const t=net.connect(target,host);t.setNoDelay(true); s.pipe(t);t.pipe(s);
 s.on('error',()=>t.destroy());t.on('error',()=>s.destroy());s.on('close',()=>t.destroy());t.on('close',()=>s.destroy()); }).listen(port,'0.0.0.0');
}
"""


def write_override(root: Path, ports: dict):
    root.mkdir(parents=True, exist_ok=True)
    write_ports_override(root)
    (root/'relay.cjs').write_text(RELAY,encoding='utf-8')
    override={'networks':{'default':{'internal':True},'exposure':{}},'services':{
        'db':{'depends_on':{'exposure':{'condition':'service_started'}}},
        'minio':{'depends_on':{'exposure':{'condition':'service_started'}}},
        'exposure':{'image':'node:24-alpine','command':['node','/relay.cjs'],
            'volumes':[(root/'relay.cjs').as_posix()+':/relay.cjs:ro'],'networks':['default','exposure'],
            'ports':[f"127.0.0.1:{ports[key]}:{port}" for key,port in (
                ('POSTGRES_HOST_PORT',5432),('API_HOST_PORT',8000),('WEB_HOST_PORT',3000),
                ('MINIO_API_HOST_PORT',9000),('MINIO_CONSOLE_HOST_PORT',9001))]}}}
    path=root/'isolation.json'
    path.write_text(json.dumps(override),encoding='utf-8')
    return path
