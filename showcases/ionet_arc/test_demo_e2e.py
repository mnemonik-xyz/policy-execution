"""End-to-end demonstration test running Anvil, MCP Server, and Client Agent."""
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from showcases.ionet_arc.escrow import find_binary

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "bin" / "python"


os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")


class EndToEndWarrantTranscriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 1. Start Anvil
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.anvil_port = s.getsockname()[1]
        cls.rpc_url = f"http://127.0.0.1:{cls.anvil_port}"
        cls.anvil_proc = subprocess.Popen(
            [find_binary("anvil"), "--host", "127.0.0.1", "--port",
             str(cls.anvil_port), "--chain-id", "31337", "--silent"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for anvil
        for _ in range(50):
            try:
                res = subprocess.run([find_binary("cast"), "chain-id", "--rpc-url",
                                     cls.rpc_url], capture_output=True, text=True)
                if res.returncode == 0 and res.stdout.strip() == "31337":
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            cls.tearDownClass()
            raise RuntimeError("Anvil failed to start")

        # 2. Start MCP server
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.mcp_port = s.getsockname()[1]
        cls.mcp_url = f"http://127.0.0.1:{cls.mcp_port}/mcp"
        cls.tmp_dir = tempfile.mkdtemp(prefix="warrant-state-")
        cls.test_audio = Path(cls.tmp_dir) / "test_sample.mp3"
        cls.test_audio.write_bytes(b"mock sample audio bytes for autonomous transcription pilot")

        cmd = [
            str(PYTHON),
            str(ROOT / "showcases" / "ionet_arc" / "seller_mcp_server.py"),
            "--host", "127.0.0.1",
            "--port", str(cls.mcp_port),
            "--state-dir", cls.tmp_dir,
            "--rpc-url", cls.rpc_url,
            "--mock-ionet",
        ]
        env = dict(os.environ)
        env["no_proxy"] = "127.0.0.1,localhost"
        env["NO_PROXY"] = "127.0.0.1,localhost"
        cls.server_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

        # Wait for server
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(50):
            try:
                with opener.open(f"http://127.0.0.1:{cls.mcp_port}/mcp", timeout=1) as r:
                    pass
            except urllib.error.HTTPError as e:
                if e.code in (400, 404, 405, 406):  # Starlette up
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            cls.tearDownClass()
            raise RuntimeError("MCP server failed to start")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "server_proc") and cls.server_proc:
            cls.server_proc.terminate()
            cls.server_proc.wait(timeout=2)
        if hasattr(cls, "anvil_proc") and cls.anvil_proc:
            cls.anvil_proc.terminate()
            cls.anvil_proc.wait(timeout=2)
        if hasattr(cls, "tmp_dir") and Path(cls.tmp_dir).exists():
            shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    def test_run_client_against_live_server_and_anvil(self):
        env = dict(os.environ)
        env["no_proxy"] = "127.0.0.1,localhost"
        env["NO_PROXY"] = "127.0.0.1,localhost"
        cmd = [
            str(PYTHON),
            str(ROOT / "showcases" / "ionet_arc" / "buyer_test_agent.py"),
            "--mcp-url", self.mcp_url,
            "--rpc-url", self.rpc_url,
            "--audio", str(self.test_audio),
        ]
        res = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, env=env)
        print("CLIENT OUTPUT:\n", res.stdout)
        if res.returncode != 0:
            print("CLIENT STDERR:\n", res.stderr)
        self.assertEqual(res.returncode, 0)
        self.assertIn("WARRANT AUTONOMOUS TRANSCRIPTION AGENT", res.stdout)
        self.assertIn("Policy evaluation: APPROVED", res.stdout)
        self.assertIn("TaskEscrow funded on-chain", res.stdout)
        self.assertIn("Deployment provisioned and escrow settled", res.stdout)
        self.assertIn("Settlement TX: 0x", res.stdout)
        self.assertIn("TRANSCRIPTION RESULT", res.stdout)
        self.assertIn("Autonomous Warrant transcription showcase completed successfully!", res.stdout)


if __name__ == "__main__":
    unittest.main()
