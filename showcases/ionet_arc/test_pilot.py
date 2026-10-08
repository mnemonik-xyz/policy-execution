import asyncio
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import socket
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch
import urllib.error
import urllib.request

from showcases.ionet_arc.adapter import (Cloud, DEPLOY, DESTROY, ENDPOINT, PilotError,
                                         decode_result, deploy, destroy, read_json)
from showcases.ionet_arc.arc import preflight
from showcases.ionet_arc.funding import SOLANA, payment_plan
from showcases.ionet_arc.worker import Worker, handler
from showcases.ionet_arc import adapter


ARGS = {"billing_model": "duration", "hardware_id": 10, "location_ids": [20],
        "duration_hours": 1, "gpus_per_container": 1, "replica_count": 1}
DEPLOYMENT_ID = "3c90c3cc-0d44-4b50-8888-8dd25736052a"


def estimate():
    # Synthetic test response, deliberately not presented as io.net's schema.
    return {"tool": "caas_get_price_estimate", "observed_at": int(time.time()),
            "arguments": ARGS.copy(), "result": {"test_total_usd": "1.25"}}


def quote_state(network="mainnet"):
    caip, mint = SOLANA[network]
    return {"endpoint": ENDPOINT, "phase": "payment_required", "created_at": int(time.time()),
            "payment": {"x402Version": 2, "accepts": [{"scheme": "exact", "network": caip,
                                                       "asset": mint, "payTo": SOLANA["testnet"][1],
                                                       "maxAmountRequired": "3820000"}]}}


class DeploymentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"
        self.cloud = AsyncMock()
        self.cloud.call.return_value = {"status": "success", "deployment_id": DEPLOYMENT_ID}

    async def launch(self, **kwargs):
        return await deploy(self.cloud, kwargs.get("args", ARGS), self.state,
                            kwargs.get("estimate", estimate()), "/test_total_usd", "2")

    async def test_success_then_duplicate_cannot_spend(self):
        result = await self.launch()
        self.assertEqual(result["phase"], "deployed")
        self.assertEqual(result["deployment_id"], DEPLOYMENT_ID)
        self.assertNotIn("arguments", read_json(self.state))
        with self.assertRaises(FileExistsError):
            await self.launch()
        self.cloud.call.assert_awaited_once_with(DEPLOY, ARGS)

    async def test_timeout_leaves_unknown_and_does_not_retry(self):
        self.cloud.call.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.launch()
        self.assertEqual(read_json(self.state)["phase"], "submission_unknown")
        with self.assertRaises(FileExistsError):
            await self.launch()
        self.assertEqual(self.cloud.call.await_count, 1)

    async def test_payment_request_is_saved_without_payment_or_retry(self):
        payment = quote_state()["payment"]
        self.cloud.call.return_value = {"status": "payment_required", "payment": payment}
        result = await self.launch()
        self.assertEqual(result["payment"], payment)
        self.assertEqual(result["phase"], "payment_required")
        self.assertEqual(self.cloud.call.await_count, 1)

    async def test_malformed_success_remains_unknown(self):
        self.cloud.call.return_value = {"status": "success"}
        with self.assertRaises(PilotError):
            await self.launch()
        self.assertEqual(read_json(self.state)["phase"], "submission_unknown")

    async def test_payg_and_unbounded_capacity_rejected_before_call(self):
        for change in ({"billing_model": "payg"}, {"duration_hours": 2}, {"replica_count": 2},
                       {"hardware_id": "gpu_1x_a6000"}, {"node_pool_id": "private"},
                       {"location_ids": []}, {"gpus_per_container": True}, {"duration_hours": -1}):
            with self.subTest(change=change), self.assertRaises(PilotError):
                await self.launch(args=dict(ARGS, **change))
        self.cloud.call.assert_not_awaited()
        self.assertFalse(self.state.exists())

    async def test_stale_mismatched_overbudget_estimates_rejected(self):
        for kind in ("stale", "changed", "expensive", "negative", "nan", "missing"):
            value = estimate()
            if kind == "stale":
                value["observed_at"] -= 301
            if kind == "changed":
                value["arguments"]["location_ids"] = [99]
            if kind == "expensive":
                value["result"]["test_total_usd"] = "2.01"
            if kind == "negative":
                value["result"]["test_total_usd"] = "-1"
            if kind == "nan":
                value["result"]["test_total_usd"] = "NaN"
            if kind == "missing":
                value["result"] = {}
            with self.subTest(kind=kind), self.assertRaises(PilotError):
                await self.launch(estimate=value)
        self.cloud.call.assert_not_awaited()

    async def test_destroy_ack_is_not_claimed_as_stopped(self):
        await self.launch()
        result = await destroy(self.cloud, self.state)
        self.assertEqual(result["phase"], "destroy_requested")
        self.cloud.call.assert_awaited_with(DESTROY, {"deployment_id": DEPLOYMENT_ID})
        with self.assertRaises(PilotError):
            await destroy(self.cloud, self.state)

    async def test_destroy_failure_keeps_deployment_id(self):
        await self.launch()
        self.cloud.call.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await destroy(self.cloud, self.state)
        state = read_json(self.state)
        self.assertEqual(state["phase"], "destroy_unknown")
        self.assertEqual(state["deployment_id"], DEPLOYMENT_ID)

    async def test_real_sdk_result_shapes_and_schema_validation(self):
        from mcp import types
        session = AsyncMock()
        session.call_tool.return_value = types.CallToolResult(
            content=[types.TextContent(type="text", text='{"status":"ok"}')])
        schemas = {"get_credit_status": {"type": "object", "additionalProperties": False}}
        cloud = Cloud(session, schemas)
        self.assertEqual(await cloud.call("get_credit_status", {}), {"status": "ok"})
        with self.assertRaises(PilotError):
            await cloud.call("get_credit_status", {"unexpected": "SECRET"})
        with self.assertRaises(PilotError):
            await cloud.call("get_credit_status", {"auth": {"api_key": "SECRET"}})
        with self.assertRaises(PilotError):
            await cloud.call("caas_extend_deployment_duration", {})
        self.assertEqual(session.call_tool.await_count, 1)

    async def test_sdk_streamable_http_discovery_and_tool_call(self):
        from mcp.server.mcpserver import MCPServer
        import uvicorn
        mcp = MCPServer("local-test-provider")

        @mcp.tool()
        def get_credit_status() -> dict:
            return {"test_credit": "10.00"}

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), log_level="error",
                                               timeout_graceful_shutdown=1))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(100):
                if server.started or task.done():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(server.started)
            with patch.object(adapter, "ENDPOINT", f"http://127.0.0.1:{port}/mcp"), \
                    patch.dict(os.environ, {"IO_NET_API_KEY": "local-test-key"}):
                result = await adapter.connected(lambda cloud: cloud.call("get_credit_status", {}))
            self.assertEqual(result, {"test_credit": "10.00"})
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
            sock.close()


class ArcAndPaymentTests(unittest.TestCase):
    def plan(self, state, network="mainnet", limit="4"):
        return payment_plan(state, network, SOLANA["mainnet"][1], limit)

    def test_arc_checks_chain_and_six_decimal_interface(self):
        replies = iter([hex(5042), hex(6)])
        result = preflight("mainnet", call=lambda *args: next(replies))
        self.assertEqual(result["native_decimals"], 18)
        self.assertFalse(result["escrow_deployed"])
        for values in ([hex(1)], [hex(5042), hex(18)]):
            replies = iter(values)
            with self.assertRaises(PilotError):
                preflight("mainnet", call=lambda *args: next(replies))

    def test_exact_payment_separate_from_bridge_fees(self):
        result = self.plan(quote_state())
        self.assertFalse(result["executable"])
        self.assertEqual(result["source"]["chain_id"], 5042)
        self.assertEqual(result["bridge"]["destination_domain"], 5)
        self.assertEqual(result["bridge"]["minimum_net_usdc"], "3.82")
        self.assertIsNone(result["bridge"]["gross_amount"])
        self.assertEqual(result["provider_payment"]["amount"], "3820000")

    def test_cross_environment_wrong_mint_and_bad_recipient_rejected(self):
        for key, value in (("network", SOLANA["testnet"][0]), ("asset", SOLANA["testnet"][1]),
                           ("scheme", "upto"), ("payTo", "0x1234"), ("payTo", "1" * 32)):
            state = quote_state()
            state["payment"]["accepts"][0][key] = value
            with self.subTest(key=key), self.assertRaises(PilotError):
                self.plan(state)
        with self.assertRaises(PilotError):
            self.plan(quote_state(), network="testnet")

    def test_amount_validation_budget_and_staleness(self):
        for amount in ("0", "-1", "1.5", "1e6", 3820000):
            state = quote_state()
            state["payment"]["accepts"][0]["maxAmountRequired"] = amount
            with self.subTest(amount=amount), self.assertRaises(PilotError):
                self.plan(state)
        for limit in ("3", "NaN", "Infinity", "-1"):
            with self.subTest(limit=limit), self.assertRaises(PilotError):
                self.plan(quote_state(), limit=limit)
        state = quote_state()
        state["created_at"] -= 301
        with self.assertRaises(PilotError):
            self.plan(state)

    def test_v2_amount_and_conflicting_legacy_field(self):
        state = quote_state()
        quote = state["payment"]["accepts"][0]
        quote["amount"] = quote.pop("maxAmountRequired")
        self.assertEqual(self.plan(state)["provider_payment"]["amount"], "3820000")
        quote["maxAmountRequired"] = "1"
        with self.assertRaises(PilotError):
            self.plan(state)

    def test_tool_failure_does_not_echo_secrets(self):
        for result in ({"isError": True, "content": [{"type": "text", "text": "SECRET"}]},
                       {"structuredContent": {"status": "error", "error": "SECRET"}},
                       {"content": [{"type": "text", "text": "SECRET"}]}):
            with self.assertRaises(PilotError) as ctx:
                decode_result(result)
            self.assertNotIn("SECRET", str(ctx.exception))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        self.done = threading.Event()

        def engine(audio, model_dir):
            self.calls.append(audio.read_bytes())
            self.done.set()
            return {"language": "en", "segments": [{"start": 0, "end": 1, "text": "test fixture"}]}
        self.worker = Worker(self.tmp.name, "test-token-" * 4, engine)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.worker))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, body=None, auth=True, checksum=None, extra_headers=None):
        headers = {}
        if auth:
            headers["Authorization"] = "Bearer " + self.worker.token
        if body is not None:
            headers["X-Audio-SHA256"] = checksum or hashlib.sha256(body).hexdigest()
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(self.url + path, body, headers)
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_language_argument_and_validation(self):
        code, err = self.request("/job?language=invalid_lang", b"audio")
        self.assertEqual(code, 400)
        code, err = self.request("/job", b"audio", extra_headers={"X-Language": "invalid_lang"})
        self.assertEqual(code, 400)

    def test_upload_poll_duplicate_and_persistent_result(self):
        code, _ = self.request("/job", b"fake audio for unit test")
        self.assertEqual(code, 202)
        self.assertTrue(self.done.wait(2))
        for _ in range(100):
            code, job = self.request("/job")
            if job["status"] == "complete":
                break
            time.sleep(.01)
        self.assertEqual(job["status"], "complete")
        data = json.dumps(job["result"], sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(job["result_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.request("/job", b"fake audio for unit test")[0], 200)
        self.assertEqual(self.request("/job", b"different audio")[0], 409)
        self.assertEqual(len(self.calls), 1)
        restored = Worker(self.tmp.name, self.worker.token)
        self.assertEqual(restored.job["status"], "complete")

    def test_auth_hash_and_health(self):
        self.assertEqual(self.request("/health", auth=False)[0], 200)
        self.assertEqual(self.request("/job", auth=False)[0], 401)
        self.assertEqual(self.request("/job", b"audio", auth=False)[0], 401)
        self.assertEqual(self.request("/job", b"audio", checksum="0" * 64)[0], 400)
        self.assertEqual(self.calls, [])

    def test_restart_marks_incomplete_job_interrupted(self):
        Path(self.tmp.name, "job.json").write_text(json.dumps({"status": "running", "input_sha256": "0" * 64}))
        restored = Worker(self.tmp.name, self.worker.token)
        self.assertEqual(restored.job["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
