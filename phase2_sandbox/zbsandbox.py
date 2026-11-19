"""Client for Zeroboot persistent sandboxes (zeroboot-pin patch 0007). Stdlib only.

    from zbsandbox import Sandbox, ZerobootRuntime
    with Sandbox(seed=1) as sb:
        r = sb.exec("python -c 'print(1)'", cwd="/testbed")
        sb.put("/tmp/x.txt", b"hello")

ZerobootRuntime mirrors the parts of R2E-Gym's DockerRuntime (run, run_tests,
copy_to_container) that an agent step touches, with byte-for-byte the same
command wrapping and output post-processing as DockerRuntime.run(), so
replayed traces see the observations they saw in docker.
"""

import base64
import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("ZEROBOOT_BASE", "http://127.0.0.1:8080")

# R2E-Gym constants (r2egym/agenthub/__init__.py, runtime/docker.py @ 0d94c4e)
CMD_TIMEOUT = 120
_ANSI = re.compile(r"\x1b\[[0-9;]*m|\r")


class SandboxError(RuntimeError):
    pass


def _call(base, method, path, body=None, timeout=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    key = os.environ.get("ZEROBOOT_API_KEY")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:  # noqa: BLE001
            msg = str(e)
        raise SandboxError(f"{method} {path}: HTTP {e.code}: {msg}") from None


class Sandbox:
    """One persistent sandbox forked from a template held by `zeroboot serve`."""

    def __init__(self, template="python", seed=None, base=DEFAULT_BASE):
        self.base = base
        body = {"template": template}
        if seed is not None:
            body["seed"] = int(seed)
        r = _call(base, "POST", "/v1/sandboxes", body, timeout=120)
        self.id, self.fork_ms, self.init_ms = r["id"], r["fork_ms"], r["init_ms"]

    def exec(self, cmd, cwd="", timeout_ms=300_000, max_output=1 << 20):
        """-> {output, exit_code, truncated, timed_out, exec_ms}. output is UTF-8 with
        replacement chars, like R2E's .decode('utf-8', errors='replace')."""
        return _call(self.base, "POST", f"/v1/sandboxes/{self.id}/exec",
                     {"cmd": cmd, "cwd": cwd, "timeout_ms": int(timeout_ms), "max_output": int(max_output)},
                     timeout=timeout_ms / 1000 + 60)

    def put(self, path, data, mode=0o644):
        if isinstance(data, str):
            data = data.encode()
        _call(self.base, "POST", f"/v1/sandboxes/{self.id}/files",
              {"path": path, "content_b64": base64.b64encode(data).decode(), "mode": f"{mode:o}"}, timeout=600)

    def close(self):
        if self.id:
            try:
                _call(self.base, "DELETE", f"/v1/sandboxes/{self.id}", timeout=30)
            except SandboxError:
                pass
            self.id = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ZerobootRuntime:
    """Drop-in for the DockerRuntime methods an R2E agent step uses."""

    def __init__(self, template="python", seed=None, repo_path="/testbed", alt_path="/root",
                 base=DEFAULT_BASE):
        self.sandbox = Sandbox(template, seed, base)
        self.repo_path, self.alt_path = repo_path, alt_path

    def run(self, code, timeout=CMD_TIMEOUT, args="", workdir=None, type=None):  # noqa: A002 (R2E's name)
        # == DockerRuntime.run: `timeout {t} {code} {args}` via /bin/sh -c in workdir,
        #    PATH=DOCKER_PATH (baked into the template's /zb-env), host-side limit t+5.
        command = f"timeout {timeout} {code} {args}"
        try:
            r = self.sandbox.exec(command, cwd=self.repo_path if workdir is None else workdir,
                                  timeout_ms=(timeout + 5) * 1000)
        except SandboxError as e:
            return f"Error: {e!r}", "-1"
        output, error_code = r["output"], r["exit_code"]
        if r["timed_out"]:          # host-side t+5 limit (R2E: concurrent.futures.TimeoutError)
            return f"The command took too long to execute (>{timeout}s)", "-1"
        if error_code == 124:
            return f"The command took too long to execute (>{timeout}s)", "-1"
        if error_code != 0:
            return output, f"Error: Exit code {error_code}"
        return _ANSI.sub("", output), str(error_code)

    def run_tests(self, timeout=300):
        output, error_code = self.run(f"bash {self.alt_path}/run_tests.sh", timeout=timeout)
        return _ANSI.sub("", output), error_code

    def copy_to_container(self, src_path, dest_path):
        with open(src_path, "rb") as f:
            data = f.read()
        self.sandbox.put(dest_path, data, os.stat(src_path).st_mode & 0o777)

    def close(self):
        self.sandbox.close()
