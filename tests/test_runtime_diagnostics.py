"""Offline Windows collector checks; never import/start the app or its databases.

Run directly: python -m unittest discover -s tests -p test_runtime_diagnostics.py -v
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collect_runtime_diagnostics.ps1"
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "requires Windows PowerShell 5.1")
class RuntimeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="collector-test-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / "space 中文 [case]"
        self.work.mkdir()

    def run_ps(self, code, *, timeout=30):
        harness = self.work / "harness.ps1"
        harness.write_text(code, encoding="utf-8-sig")
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
            capture_output=True, timeout=timeout,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))

    def functions(self):
        # Load function definitions using the PS parser, without running the collector.
        quoted = str(SCRIPT).replace("'", "''")
        work = str(self.work).replace("'", "''")
        return f"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile('{quoted}', [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw ($errors | Out-String) }}
$ast.FindAll({{ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }}, $false) |
    ForEach-Object {{ Invoke-Expression $_.Extent.Text }}
$reportDir = '{work}'
$versionCache = @{{}}
"""

    def read_json(self, name):
        return json.loads((self.work / name).read_text(encoding="utf-8-sig"))

    def test_redaction_keeps_json_valid_and_memory_error_visible(self):
        self.assertTrue(SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf"), "PS 5.1 needs a BOM for Chinese")
        self.run_ps(self.functions() + r'''
$value = [ordered]@{
    error = "Out of Memory`r`nCookie: COOKIE_SENTINEL`r`nCommitLimit: 123"
    nested = @([pscustomobject]@{ message = 'GET https://host/path?key=QUERY_SENTINEL'; password = 'PASSWORD_SENTINEL' })
    secret = 'SECRET_SENTINEL'
    details = 'mysql://root:URI_SENTINEL@db.example/test'
    profile = 'C:\Users\PERSON_SENTINEL\AppData\Local'
    pem = "-----BEGIN PRIVATE KEY-----`nPEM_SENTINEL`n-----END PRIVATE KEY-----"
    continued = "password:`r`n  'CONTINUED_SENTINEL'"
    escaped = '{"accessKeySecret":"ESCAPED_SENTINEL"}'
    empty = @(); absent = $null
}
Save-Json 'protected.json' $value
''')
        value = self.read_json("protected.json")
        text = json.dumps(value)
        self.assertNotIn("SENTINEL", text)
        self.assertIn("Out of Memory", text)
        self.assertIn("CommitLimit: 123", text)
        self.assertEqual(value["empty"], [])
        self.assertIsNone(value["absent"])
        self.assertIsInstance(value["nested"], list)

    def test_tail_is_bounded_shared_and_handles_utf16_and_giant_line(self):
        (self.work / "oversize.log").write_bytes(b"X" * (3 * 1024 * 1024) + b"PRIVATE_TAIL")
        (self.work / "unicode.log").write_text("旧行\n" * 100 + "Out of Memory 内存不足\n", encoding="utf-16")
        self.run_ps(self.functions() + r'''
$locked = [IO.File]::Open((Join-Path $reportDir 'unicode.log'), 'Open', 'ReadWrite', [IO.FileShare]::ReadWrite)
try {
    Save-Json 'tails.json' @{
        huge = Read-LogTail (Join-Path $reportDir 'oversize.log') 1024
        unicode = Read-LogTail (Join-Path $reportDir 'unicode.log') 100
    }
} finally { $locked.Dispose() }
''')
        value = self.read_json("tails.json")
        self.assertIn("Oversized single line omitted", value["huge"])
        self.assertNotIn("PRIVATE_TAIL", value["huge"])
        self.assertIn("Out of Memory 内存不足", value["unicode"])
        self.assertLess(len(value["unicode"]), 200)

    def test_process_tree_excludes_unrelated_webviews_and_raw_commands(self):
        self.run_ps(self.functions() + r'''
function Make-Process($id, $parent, $name, $command) {
    [pscustomobject]@{ Name=$name; ProcessId=$id; ParentProcessId=$parent; CommandLine=$command
        CreationDate=(Get-Date); WorkingSetSize=123456; PrivatePageCount=456789; VirtualSize=987654
        PageFileUsage=44; HandleCount=11; ThreadCount=2; ExecutablePath=$null }
}
$processes = @(
    (Make-Process 10 1 'WeChat MP Tools.exe' 'COMMAND_SENTINEL'),
    (Make-Process 11 10 'msedgewebview2.exe' '--type=renderer --token=RENDERER_SENTINEL'),
    (Make-Process 12 11 'worker.exe' 'WORKER_SENTINEL'),
    (Make-Process 20 1 'msedgewebview2.exe' 'UNRELATED_SENTINEL'),
    (Make-Process 30 1 'python.exe' 'python.exe "D:\project\main.py" --password=PYTHON_SENTINEL')
)
Save-Json 'processes.json' (Get-ProcessEvidence $processes @('D:\project'))
''')
        value = self.read_json("processes.json")
        self.assertTrue(value["application_found"])
        related = {item["pid"]: item for item in value["related_processes"]}
        self.assertEqual(set(related), {10, 11, 12, 30})
        self.assertEqual(related[11]["role"], "renderer")
        self.assertEqual(related[11]["private_bytes"], 456789)
        self.assertEqual(value["all_webview_process_count"], 2)
        for item in value["top_memory_processes"]:
            self.assertEqual(item["private_bytes"], 456789)
            self.assertEqual(item["working_set_bytes"], 123456)
        self.assertNotIn("SENTINEL", json.dumps(value))
        self.assertNotIn("CommandLine", json.dumps(value))

    def test_pe_architecture_without_executing_file(self):
        pe = bytearray(128)
        pe[:2] = b"MZ"
        struct.pack_into("<I", pe, 0x3C, 64)
        struct.pack_into("<IH", pe, 64, 0x4550, 0x014C)
        (self.work / "fake.exe").write_bytes(pe)
        self.run_ps(self.functions() + "Save-Json 'pe.json' (Get-ExecutableInfo (Join-Path $reportDir 'fake.exe'))")
        self.assertEqual(self.read_json("pe.json")["architecture"], "x86")

    def test_monitor_preserves_history_baseline_and_renderer_replacement(self):
        self.run_ps(self.functions() + r'''
$script:processQueryNumber = 0
function Get-CimInstance([string]$ClassName, [int]$OperationTimeoutSec) {
    if ($ClassName -eq 'Win32_PerfFormattedData_PerfOS_Memory') {
        return [pscustomobject]@{ AvailableBytes=123456; CommittedBytes=345678; CommitLimit=999999; PercentCommittedBytesInUse=34 }
    }
    $script:processQueryNumber++
    $rendererId = 11
    if ($script:processQueryNumber -eq 3) { $rendererId = 12 }
    [pscustomobject]@{ Name='WeChat MP Tools.exe'; ProcessId=10; ParentProcessId=1; CommandLine='COMMAND_SENTINEL'
        CreationDate=[datetime]'2026-09-30T10:00:00'; WorkingSetSize=100; PrivatePageCount=200; VirtualSize=300
        PageFileUsage=0; HandleCount=2; ThreadCount=3; ExecutablePath=$null }
    [pscustomobject]@{ Name='msedgewebview2.exe'; ProcessId=$rendererId; ParentProcessId=10; CommandLine='--type=renderer --token=SECRET_SENTINEL'
        CreationDate=[datetime]'2026-10-19T10:00:00'; WorkingSetSize=200; PrivatePageCount=($script:processQueryNumber*1000000); VirtualSize=300
        PageFileUsage=0; HandleCount=2; ThreadCount=3; ExecutablePath=$null }
}
Start-MemoryMonitor $reportDir @('D:\project') -IntervalSeconds 0 -SampleLimit 3 -NoConsole
''')
        history = [json.loads(line) for line in (self.work / "memory-history.jsonl").read_text(encoding="utf-8-sig").splitlines()]
        self.assertEqual(len(history), 3)
        self.assertFalse(history[0]["renderer_identity_changed"])
        self.assertFalse(history[1]["renderer_identity_changed"])
        self.assertTrue(history[2]["renderer_identity_changed"])
        self.assertTrue(history[2]["previous_renderer_identities"][0].startswith("11@"))
        self.assertEqual(history[2]["related_processes"][1]["private_bytes"], 3000000)
        self.assertEqual(self.read_json("monitor-baseline.json")["number"], 1)
        self.assertEqual(self.read_json("monitor-last.json")["number"], 3)
        status = self.read_json("monitor-status.json")
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["stop_reason"], "sample_limit")
        self.assertEqual(status["samples"], 3)
        self.assertNotIn("SENTINEL", json.dumps(history))

    def test_memory_history_rotation_only_touches_generated_files(self):
        untouched = self.work / "application.log"
        untouched.write_bytes(b"KEEP THIS SOURCE LOG")
        self.run_ps(self.functions() + r'''
foreach ($number in 0..19) {
    Add-MemoryRecord $reportDir @{ number=$number; padding=('x' * 100) } -MaxBytes 180
}
''')
        files = sorted(self.work.glob("memory-history.jsonl*"))
        self.assertEqual(len(files), 4)
        self.assertTrue(all(p.stat().st_size <= 180 for p in files))
        records = [json.loads(line) for p in files for line in p.read_text(encoding="utf-8").splitlines()]
        self.assertEqual({r["number"] for r in records}, {16, 17, 18, 19})
        self.assertEqual(untouched.read_bytes(), b"KEEP THIS SOURCE LOG")

    def test_real_monitor_stops_at_duration_and_packages_history(self):
        tool = self.work / "tool"
        install = self.work / "install"
        output = self.work / "output"
        for folder in (tool, install, output):
            folder.mkdir()
        shutil.copy2(SCRIPT, tool / SCRIPT.name)
        (install / "wechat_mp_tools.log").write_text("fixture startup log", encoding="utf-8")
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(tool / SCRIPT.name),
             "-InstallDir", str(install), "-OutputDir", str(output), "-Monitor", "-MonitorMinutes", "0.1",
             "-SampleIntervalSeconds", "5", "-NonInteractive"],
            capture_output=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
            records = {Path(n).name: archive.read(n).decode("utf-8-sig") for n in archive.namelist() if not n.endswith("/")}
            status = json.loads(records["monitor-status.json"])
            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["stop_reason"], "duration_elapsed")
            self.assertGreaterEqual(status["samples"], 1)
            history = [json.loads(line) for line in records["memory-history.jsonl"].splitlines()]
            self.assertEqual(len(history), status["samples"])
            self.assertEqual(json.loads(records["monitor-baseline.json"])["number"], 1)
            self.assertIn("CommittedBytes", history[0]["system"])
            self.assertTrue(json.loads(records["collection-status.json"])["continuous_memory_recording"])

    def test_real_collection_zip_excludes_credentials_and_does_not_change_sources(self):
        tool = self.work / "tool"
        install = self.work / "install"
        output = self.work / "output"
        for folder in (tool, install / "data" / "logs", output):
            folder.mkdir(parents=True)
        shutil.copy2(SCRIPT, tool / SCRIPT.name)
        logs = {
            "wechat_mp_tools.log": "Startup error: Out of Memory\npassword=LOG_SENTINEL\n",
            "data/frontend_errors.log": "JS error\nhttps://example.com/path?token=URL_SENTINEL\n",
            "data/proxy_runtime.log.1": "Proxy healthy\nCookie: COOKIE_SENTINEL\n",
            "data/logs/channels_environment.jsonl": '{"event":"heartbeat_timeout"}\n',
            "data/channels_call_log.jsonl": '{"accessKeyId":"ACCESS_SENTINEL"}\n',
        }
        for name, value in logs.items():
            (install / name).write_text(value, encoding="utf-8")
        (install / "data" / "wechat_mp_config.json").write_text('"CONFIG_SENTINEL"', encoding="utf-8")
        (install / "data" / "channels_data.sqlite3").write_bytes(b"DATABASE_SENTINEL")
        (install / "data" / "ca.key").write_bytes(b"PRIVATE_KEY_SENTINEL")
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in install.rglob("*") if p.is_file()}
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(tool / SCRIPT.name),
             "-InstallDir", str(install), "-OutputDir", str(output), "-NonInteractive"],
            capture_output=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        archives = list(output.glob("*.zip"))
        self.assertEqual(len(archives), 1)
        with zipfile.ZipFile(archives[0]) as archive:
            names = archive.namelist()
            self.assertTrue(any(n.endswith("memory-and-processes.json") for n in names))
            contents = {n: archive.read(n).decode("utf-8-sig") for n in names if not n.endswith("/")}
            leaked = [name for name, value in contents.items() if "SENTINEL" in value]
            self.assertFalse(leaked, f"Unredacted test credential in: {leaked}")
            self.assertIn("Out of Memory", "\n".join(contents.values()))
            parsed = {Path(n).name: json.loads(v) for n, v in contents.items() if n.endswith(".json")}
            self.assertEqual(parsed["collection-status.json"]["collected_logs"], len(logs))
            self.assertEqual(len(parsed["memory-and-processes.json"]), 3)
            self.assertIn("system", parsed["memory-and-processes.json"][0])
            self.assertFalse(any("config.json" in n or ".sqlite3" in n or "ca.key" in n for n in names))
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in install.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
