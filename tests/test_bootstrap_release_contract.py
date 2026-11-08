"""Execute the real candidate smoke shell with local Docker/jq substitutes only."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def smoke_script(root=ROOT, step_name="Smoke test candidate images"):
    text = (root / ".github/workflows/docker.yml").read_text()
    step = text.split(f"      - name: {step_name}\n", 1)[1]
    block = step.split("        run: |\n", 1)[1].split("\n      - name:", 1)[0]
    return "\n".join(line[10:] for line in block.splitlines()) + "\n"


STUB = r'''
import json, os, pathlib, signal, sys
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
root = pathlib.Path(os.environ['STUB_ROOT'])
mode = os.environ['STUB_MODE']
def record(event):
    with (root / 'events.jsonl').open('a') as f:
        f.write(json.dumps(event) + '\n')
def opt(key):
    return args[args.index(key) + 1]
def fail(stage, code):
    if stage in mode.split('+'):
        raise SystemExit(code)
if name == 'seq':
    print('1')  # One iteration reaches the real exhaustion branch without waiting.
elif name == 'sleep' or name == 'sudo':
    pass
elif name == 'curl':
    event = {'tool': 'curl', 'args': args}
    record(event)
    if args[-1].endswith('/setup'):
        fail('readiness', 1)
    if args[-1].endswith('/readyz'):
        assert (root / 'web-ready').exists(), 'readiness was checked before Web installation'
elif name == 'jq':
    if '-r' in args:
        print('sha256:' + opt('arch') + '-fixture')
    else:
        fail('metadata', 31)
elif name == 'docker':
    event = {'args': args}
    if args[:2] == ['volume', 'create']:
        fail('volume', 32)
        count = len(list(root.glob('volume-*')))
        volume = 'owned-volume-' + str(count)
        (root / ('volume-' + str(count))).touch()
        event['volume'] = volume
        record(event)
        print(volume)
    elif args[:2] == ['volume', 'rm']:
        record(event)
        fail('cleanup', 39)
    elif args[0] == 'pull':
        record(event)
        fail('pull', 30)
    elif args[0] == 'run':
        if '--cidfile' in args:
            pathlib.Path(opt('--cidfile')).write_text('owned-' + pathlib.Path(opt('--cidfile')).stem)
        if 'version' in args:
            record(event)
            print('{}')
        else:
            record(event)
            fail('start', 38)
            print('owned-start')
    elif args[0] == 'inspect':
        record(event)
        if 'Running' in opt('--format'):
            print('false' if mode == 'exited' else 'true')
        else:
            print('137' if mode == 'shutdown' else '143')
    elif args[0] == 'exec':
        if '-i' not in args:
            record(event)
            if 'mediaflux.py' in args:
                print('[]' if 'doctor' in args else '{}')
            elif 'sh' in args:
                if 'wc -c' in args[-1]:
                    print('1')
            elif '/app/app/agent/kernel/sandbox_child.py' in args:
                assert args[-1] == '--check'
                fail('sandbox', 43)
            else:
                fail('readiness', 1)
        else:
            import urllib.parse, urllib.request
            payload = sys.stdin.read()
            event.update(script=payload, requests=[])
            class Response:
                def __init__(self, code, body='', location=None):
                    self.code, self.body = code, body
                    self.headers = {'Location': location} if location else {}
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    pass
                def read(self):
                    return self.body.encode()
            class Client:
                phase = 'fresh'
                password = None
                def open(self, req, timeout):
                    path = urllib.parse.urlparse(req.full_url).path
                    fields = {k: v[0] for k, v in urllib.parse.parse_qs((req.data or b'').decode()).items()}
                    event['requests'].append({'path': path, 'method': req.get_method(), 'fields': fields})
                    assert timeout == 15
                    assert req.headers['Origin'] == 'http://127.0.0.1:1258'
                    if path == '/setup' and not fields:
                        if mode in ('interrupt', 'terminate'):
                            record(event)
                            os.kill(os.getppid(), signal.SIGINT if mode == 'interrupt' else signal.SIGTERM)
                            raise SystemExit(0)
                        assert self.phase == 'fresh'
                        return Response(200, '<input name="csrf_token" value="setup-csrf">')
                    if path == '/setup':
                        self.password = fields['password']
                        event['settings'] = {'ENV_WEB_PASSPORT': fields['username'], 'ENV_WEB_PASSWORD': self.password}
                        if 'setup' in mode.split('+'):
                            print(self.password, file=sys.stderr)  # Noisy failure must stay out of CI logs.
                            raise SystemExit(37)
                        assert self.phase == 'fresh'
                        assert fields == {'username': 'admin', 'password': self.password,
                                          'password_confirm': self.password, 'csrf_token': 'setup-csrf'}
                        self.phase = 'login'
                        return Response(303, location='/login')
                    if path == '/login' and not fields:
                        assert self.phase == 'login'
                        return Response(200, '<input name="csrf_token" value="login-csrf">')
                    if path == '/login':
                        fail('login', 41)
                        assert self.phase == 'login'
                        assert fields == {'username': 'admin', 'password': self.password, 'csrf_token': 'login-csrf'}
                        self.phase = 'ready'
                        return Response(302, location='/')
                    assert path == '/readyz' and self.phase == 'ready'
                    return Response(200)
            urllib.request.build_opener = lambda *_: Client()
            # Represent the container's /app/db using this test's private namespace.
            payload = payload.replace('Path("/app/db")', 'Path(' + repr(str(root / 'container-data')) + ')')
            try:
                exec(compile(payload, '<docker-exec-web-smoke>', 'exec'), {'__name__': '__main__'})
                (root / 'web-ready').touch()
            finally:
                record(event)
    elif args[0] == 'logs':
        record(event)
        print('not complete' if mode == 'shutdown-log' else 'Application shutdown complete')
    elif args[0] == 'stop':
        record(event)
        fail('stop', 40)
    elif args[0] == 'rm':
        record(event)
        if '--force' in args:
            fail('cleanup', 39)
    else:
        raise AssertionError('Unrecognized Docker operation')
else:
    raise AssertionError('Unrecognized stub')
'''


class BootstrapReleaseContractTests(unittest.TestCase):
    def run_smoke(self, mode="success", root=ROOT, step_name="Smoke test candidate images"):
        with tempfile.TemporaryDirectory(prefix="bootstrap-smoke-") as raw:
            temp = Path(raw)
            tools = temp / "bin"
            tools.mkdir()
            for name in ("docker", "jq", "seq", "sleep", "sudo", "curl"):
                tool = tools / name
                tool.write_text("#!" + sys.executable + "\n" + STUB)
                tool.chmod(0o700)
            # Restricted PATH: never fall through to a real Docker/network client.
            for name, target in {
                "python3": sys.executable, "mktemp": "/usr/bin/mktemp",
                "rm": "/usr/bin/rm", "grep": "/usr/bin/grep",
                "mkdir": "/usr/bin/mkdir", "wc": "/usr/bin/wc",
            }.items():
                (tools / name).symlink_to(target)
            env = {
                "PATH": str(tools), "HOME": str(temp), "RUNNER_TEMP": str(temp),
                "TMPDIR": str(temp), "STUB_ROOT": str(temp), "STUB_MODE": mode,
                "IMAGE_REPOSITORY": "invalid.example/candidate", "IMAGE_DIGEST": "fixture",
                "EXPECTED_VERSION": "fixture", "EXPECTED_SHA": "fixture", "SOURCE_SHA": "fixture",
                "UNRELATED_PRIVATE_VALUE": "must-not-enter-web-setup",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            for relative in ("packaging/scripts/web_setup_smoke.py", "app/static/js/app.js"):
                target = temp / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((root / relative).read_bytes())
            script = smoke_script(root, step_name)
            proc = subprocess.Popen(
                ["/usr/bin/bash", "--noprofile", "--norc", "-c", script],
                env=env, cwd=temp, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=15)
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
            trace = temp / "events.jsonl"
            events = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
            private_leftovers = list(temp.glob("mediaflux-smoke.*"))
            self.assertFalse(private_leftovers, "private smoke directory leaked")
            for event in events:
                for key, value in event.get("settings", {}).items():
                    if key in ("WEB_SECRET_KEY", "ENV_WEB_PASSWORD"):
                        self.assertNotIn(value, stdout + stderr)
                        self.assertNotIn(value, json.dumps([e["args"] for e in events]))
            return proc.returncode, events

    def assert_cleaned(self, events):
        created = [e["volume"] for e in events if "volume" in e]
        removed = [e["args"][2] for e in events if e["args"][:2] == ["volume", "rm"]]
        self.assertEqual(removed, created)
        for event in events:
            args = event["args"]
            self.assertNotIn("prune", args)
            if args[0] == "rm":
                self.assertTrue(args[-1].startswith(("owned-", "mediaflux-")))
        for event in events:
            args = event["args"]
            if args[0] == "run" and "--cidfile" in args:
                cid = "owned-" + Path(args[args.index("--cidfile") + 1]).stem
                self.assertTrue(any(e["args"][0] == "rm" and e["args"][-1] == cid for e in events))

    def test_one_start_then_web_setup_and_login_on_each_platform(self):
        code, events = self.run_smoke()
        self.assertEqual(code, 0)
        starts = [e for e in events if '--detach' in e['args']]
        forms = [e for e in events if 'requests' in e]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(forms), 2)
        volumes = []
        for platform, start, form in zip(('linux/amd64', 'linux/arm64'), starts, forms):
            args = start['args']
            self.assertEqual(args[args.index('--platform') + 1], platform)
            self.assertEqual(args[args.index('--user') + 1], '0:0')
            selected_env = [args[i + 1] for i, value in enumerate(args) if value == '--env']
            self.assertEqual(set(selected_env), {'MEDIAFLUX_DATA_DIR=/app/db', 'MEDIAFLUX_CONFIG_DIR=/app/db'})
            self.assertEqual(form['args'], ['exec', '-i', args[args.index('--name') + 1], 'python', '-'])
            self.assertLess(events.index(start), events.index(form))
            self.assertEqual([(r['method'], r['path']) for r in form['requests']], [
                ('GET', '/setup'), ('POST', '/setup'), ('GET', '/login'),
                ('POST', '/login'), ('GET', '/readyz'),
            ])
            self.assertEqual(set(form['settings']), {'ENV_WEB_PASSPORT', 'ENV_WEB_PASSWORD'})
            self.assertNotIn('must-not-enter-web-setup', json.dumps(form))
            volumes.append(args[args.index('--mount') + 1])
        self.assertEqual(len(set(volumes)), 2)
        for event in events:
            self.assertNotIn('initialize', event['args'])
            self.assertNotIn('restart', event['args'])
        self.assert_cleaned(events)

    def test_fail_closed_and_cleanup_preserve_exit_codes(self):
        for mode, expected in (('pull', 30), ('metadata', 31), ('volume', 32),
                               ('start', 38), ('exited', 1), ('readiness', 1),
                               ('setup', 37), ('login', 41), ('stop', 40),
                               ('shutdown', 1), ('shutdown-log', 1), ('cleanup', 0),
                               ('setup+cleanup', 37), ('interrupt', 130), ('terminate', 143)):
            with self.subTest(mode=mode):
                code, events = self.run_smoke(mode)
                self.assertEqual(code, expected)
                if mode in ('pull', 'metadata', 'volume'):
                    self.assertFalse(any('--detach' in e['args'] for e in events))
                if mode in ('start', 'exited', 'readiness'):
                    self.assertFalse(any('requests' in e for e in events))
                self.assert_cleaned(events)


if __name__ == "__main__":
    unittest.main()
