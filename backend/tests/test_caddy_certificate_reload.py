"""执行真实入口脚本，用隔离命令替身验证外部证书更新触发reload。"""
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path


def test_external_certificate_change_reloads_caddy(tmp_path):
    scripts = tmp_path / "bin"
    scripts.mkdir()
    certificate = tmp_path / "public-certificate"
    certificate.write_text("certificate-one")
    log, stop, ready = tmp_path / "commands", tmp_path / "stop", tmp_path / "ready"
    commands = {
        "sleep": '#!/bin/sh\nexec /bin/sleep 0.05\n',
        "sha256sum": f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c '\
            + shlex.quote('import hashlib,os,pathlib; data=open(os.environ["TEST_CERT"],"rb").read(); print(hashlib.sha256(data).hexdigest()); pathlib.Path(os.environ["TEST_READY"]).touch()') + '\n',
        "caddy": '#!/bin/sh\nif [ "$1" = run ]; then\n'
            ' while [ ! -f "$TEST_STOP" ]; do /bin/sleep 0.05; done\n'
            'else\n echo "$*" >> "$TEST_LOG"\nfi\n',
    }
    for name, source in commands.items():
        path = scripts / name
        path.write_text(source)
        path.chmod(0o755)
    environment = {**os.environ, "PATH": f"{scripts}:{os.environ.get('PATH', '')}",
                   "TEST_CERT": str(certificate), "TEST_STOP": str(stop), "TEST_LOG": str(log), "TEST_READY": str(ready)}
    process = subprocess.Popen(["/bin/sh", str(Path(__file__).resolve().parents[2] / "scripts/run_caddy.sh")],
                               env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "入口没有读取初始证书"
        certificate.write_text("certificate-two")
        deadline = time.monotonic() + 5
        while not log.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert log.exists(), "证书变化后没有调用reload"
        assert "reload --config /etc/caddy/Caddyfile --adapter caddyfile --force" in log.read_text()
    finally:
        stop.touch()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
    assert process.returncode == 0
