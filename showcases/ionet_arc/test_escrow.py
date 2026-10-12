import asyncio
import unittest
from unittest.mock import MagicMock, patch

from eth_abi import encode
from eth_account import Account
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from showcases.ionet_arc.escrow import (
    EscrowClient,
    EthRpc,
    compile_buyer_policy,
    compute_task_id,
    encode_journal,
    ensure_policy_hash,
    parse_public_key,
    should_trust_env,
)


class EscrowClientTests(unittest.TestCase):
    def setUp(self):
        self.rpc_url = "http://127.0.0.1:8545"
        self.escrow_addr = "0x" + "11" * 20
        self.token_addr = "0x" + "22" * 20
        self.verifier_addr = "0x" + "33" * 20
        self.server_account = "0x" + "44" * 20
        self.client = EscrowClient(
            rpc_url=self.rpc_url,
            escrow_address=self.escrow_addr,
            token_address=self.token_addr,
            verifier_address=self.verifier_addr,
            server_account=self.server_account,
        )
        self.mock_rpc = MagicMock()
        self.mock_rpc.chain_id.return_value = 31337
        self.client.rpc = self.mock_rpc

    def test_encode_journal_384_bytes(self):
        journal = encode_journal(
            policy_hash="0x" + "aa" * 32,
            chain_id=31337,
            vault="0x" + "11" * 20,
            token="0x" + "22" * 20,
            recipient="0x" + "33" * 20,
            amount=1_000_000,
            task_id="0x" + "44" * 32,
            deliverable_hash="0x" + "55" * 32,
            policy_version=1,
            valid_after=1000,
            valid_until=5000,
            evidence_hash="0x" + "66" * 32,
        )
        self.assertEqual(len(journal), 384)
        self.assertEqual(journal[:32], bytes.fromhex("aa" * 32))

    def test_get_task_decodes_struct_correctly(self):
        task_id = "0x" + "99" * 32
        customer = to_checksum_address("0x" + "aa" * 20)
        recipient = to_checksum_address("0x" + "bb" * 20)
        policy_hash = bytes.fromhex("cc" * 32)
        policy_version = 1
        amount = 1_000_000
        accept_by = 1700000000
        settle_by = 1700086400
        state_idx = 1  # Offered

        types = ["address", "address", "bytes32", "uint64", "uint64", "uint64", "uint64", "uint8"]
        vals = [customer, recipient, policy_hash, policy_version, amount, accept_by, settle_by, state_idx]
        raw_calldata = encode(types, vals)

        self.mock_rpc.rpc.return_value = "0x" + raw_calldata.hex()

        task = self.client.get_task(task_id)
        self.assertEqual(task["task_id"], task_id)
        self.assertEqual(task["customer"], customer)
        self.assertEqual(task["recipient"], recipient)
        self.assertEqual(task["policy_hash"], "0x" + "cc" * 32)
        self.assertEqual(task["policy_version"], 1)
        self.assertEqual(task["amount"], 1_000_000)
        self.assertEqual(task["accept_by"], 1700000000)
        self.assertEqual(task["settle_by"], 1700086400)
        self.assertEqual(task["state_idx"], 1)
        self.assertEqual(task["state"], "Offered")

        # Verify selector used was tasks(bytes32)
        expected_selector = function_signature_to_4byte_selector("tasks(bytes32)")
        call_args = self.mock_rpc.rpc.call_args[0]
        self.assertEqual(call_args[0], "eth_call")
        payload = call_args[1][0]
        self.assertTrue(payload["data"].startswith("0x" + expected_selector.hex()))

    def test_token_balance_decodes_uint256(self):
        account = "0x" + "aa" * 20
        raw_balance = encode(["uint256"], [5_000_000])
        self.mock_rpc.rpc.return_value = "0x" + raw_balance.hex()

        bal = self.client.token_balance(account)
        self.assertEqual(bal, 5_000_000)

        expected_selector = function_signature_to_4byte_selector("balanceOf(address)")
        call_args = self.mock_rpc.rpc.call_args[0]
        payload = call_args[1][0]
        self.assertTrue(payload["data"].startswith("0x" + expected_selector.hex()))

    def test_task_id_for_decodes_bytes32(self):
        customer = "0x" + "aa" * 20
        salt = "0x" + "bb" * 32
        expected_task_id = bytes.fromhex("77" * 32)
        self.mock_rpc.rpc.return_value = "0x" + encode(["bytes32"], [expected_task_id]).hex()

        res = self.client.task_id_for(customer, salt)
        self.assertEqual(res, "0x" + "77" * 32)
        self.assertEqual(self.mock_rpc.rpc.call_count, 1)

        # Second call with identical arguments must hit cache without additional RPC calls
        res_cached = self.client.task_id_for(customer, salt)
        self.assertEqual(res_cached, "0x" + "77" * 32)
        self.assertEqual(self.mock_rpc.rpc.call_count, 1)

    def test_compute_task_id_deterministic(self):
        customer = "0x" + "aa" * 20
        salt = "0x" + "bb" * 32
        chain_id = 31337
        escrow = "0x" + "11" * 20
        tid = compute_task_id(chain_id, escrow, customer, salt)
        self.assertTrue(tid.startswith("0x"))
        self.assertEqual(len(tid), 66)
        # Verify determinism
        self.assertEqual(tid, compute_task_id(chain_id, escrow, customer, salt))

    def test_task_id_for_fallback_on_rpc_failure(self):
        customer = "0x" + "aa" * 20
        salt = "0x" + "cc" * 32
        self.mock_rpc.rpc.side_effect = RuntimeError("RPC unreachable")
        self.mock_rpc.chain_id.return_value = 31337

        tid = self.client.task_id_for(customer, salt)
        expected = compute_task_id(31337, self.client.escrow, customer, salt)
        self.assertEqual(tid, expected)

    def test_eth_rpc_user_agent_header(self):
        import io
        from unittest.mock import MagicMock

        rpc = EthRpc("http://127.0.0.1:8545")
        mock_resp = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"result":"0x1"}')
        captured_req = []

        def mock_open(req, timeout=15):
            captured_req.append(req)
            return mock_resp

        mock_opener = MagicMock()
        mock_opener.open.side_effect = mock_open
        rpc._opener = mock_opener

        res = rpc.rpc("eth_blockNumber")
        self.assertEqual(res, "0x1")
        self.assertEqual(len(captured_req), 1)
        req = captured_req[0]
        self.assertIn("User-agent", req.headers)
        self.assertIn("WarrantClient", req.headers["User-agent"])

    def test_send_transaction_unlocked_mode(self):
        from_account = "0x" + "aa" * 20
        to_address = "0x" + "bb" * 20
        calldata = b"\x12\x34\x56\x78"

        def mock_rpc_side_effect(method, params=None):
            if method == "eth_sendTransaction":
                return "0x" + "ee" * 32
            if method == "eth_getTransactionReceipt":
                return {"transactionHash": "0x" + "ee" * 32, "status": "0x1"}
            return None

        self.mock_rpc.rpc.side_effect = mock_rpc_side_effect

        receipt = self.client.send_transaction(from_account, to_address, calldata)
        self.assertEqual(receipt["status"], "0x1")
        self.assertEqual(receipt["transactionHash"], "0x" + "ee" * 32)

        # Check eth_sendTransaction call
        calls = [c[0] for c in self.mock_rpc.rpc.call_args_list]
        self.assertEqual(calls[0][0], "eth_sendTransaction")
        tx_data = calls[0][1][0]
        self.assertEqual(tx_data["from"], to_checksum_address(from_account))
        self.assertEqual(tx_data["to"], to_checksum_address(to_address))
        self.assertEqual(tx_data["data"], "0x12345678")

    def test_send_transaction_signed_private_key_mode(self):
        acc = Account.create()
        self.client.register_key(acc.address, acc.key.hex())

        to_address = "0x" + "bb" * 20
        calldata = b"\x12\x34\x56\x78"

        def mock_rpc_side_effect(method, params=None):
            if method == "eth_getTransactionCount":
                return "0x0"
            if method == "eth_chainId":
                return "0x7a69"  # 31337
            if method == "eth_gasPrice":
                return "0x3b9aca00"  # 1 gwei
            if method == "eth_estimateGas":
                return "0x5208"  # 21000
            if method == "eth_sendRawTransaction":
                return "0x" + "ff" * 32
            if method == "eth_getTransactionReceipt":
                return {"transactionHash": "0x" + "ff" * 32, "status": "0x1"}
            return None

        self.mock_rpc.rpc.side_effect = mock_rpc_side_effect

        receipt = self.client.send_transaction(acc.address, to_address, calldata)
        self.assertEqual(receipt["status"], "0x1")
        self.assertEqual(receipt["transactionHash"], "0x" + "ff" * 32)

        # Check eth_sendRawTransaction was used
        methods = [c[0][0] for c in self.mock_rpc.rpc.call_args_list]
        self.assertIn("eth_sendRawTransaction", methods)
        self.assertNotIn("eth_sendTransaction", methods)

    def test_offer_encodes_and_sends(self):
        with patch.object(self.client, "send_transaction", return_value={"status": "0x1"}) as mock_send:
            self.client.offer(
                salt="0x" + "11" * 32,
                recipient="0x" + "22" * 20,
                policy_hash="0x" + "33" * 32,
                policy_version=1,
                amount=1_000_000,
                accept_by=1700000000,
                settle_by=1700086400,
                sender="0x" + "44" * 20,
            )
            mock_send.assert_called_once()
            args = mock_send.call_args[0]
            self.assertEqual(args[0], "0x" + "44" * 20)
            self.assertEqual(args[1], self.escrow_addr)
            # Check selector for offer
            expected_selector = function_signature_to_4byte_selector(
                "offer(bytes32,address,bytes32,uint64,uint64,uint64,uint64)"
            )
            self.assertEqual(args[2][:4], expected_selector)

    def test_accept_encodes_and_sends(self):
        with patch.object(self.client, "send_transaction", return_value={"status": "0x1"}) as mock_send:
            self.client.accept("0x" + "99" * 32, sender="0x" + "44" * 20)
            mock_send.assert_called_once()
            args = mock_send.call_args[0]
            self.assertEqual(args[0], "0x" + "44" * 20)
            self.assertEqual(args[1], self.escrow_addr)
            expected_selector = function_signature_to_4byte_selector("accept(bytes32)")
            self.assertEqual(args[2][:4], expected_selector)

    def test_should_trust_env(self):
        self.assertFalse(should_trust_env("http://127.0.0.1:8545"))
        self.assertFalse(should_trust_env("http://localhost:8000"))
        self.assertFalse(should_trust_env("http://[::1]:8080"))
        self.assertFalse(should_trust_env("127.0.0.1"))
        self.assertFalse(should_trust_env("localhost"))
        self.assertFalse(should_trust_env(None))
        self.assertFalse(should_trust_env(""))
        self.assertTrue(should_trust_env("http://petertower:8080"))
        self.assertTrue(should_trust_env("https://mcp.io.solutions/mcp"))
        self.assertTrue(should_trust_env("remote.example.com"))

    def test_eth_rpc_chain_id_and_accounts_caching(self):
        rpc = EthRpc("http://mock-chain-test.internal:8545")
        with patch.object(rpc, "rpc") as mock_call:
            mock_call.side_effect = lambda method, params=None: "0x7a69" if method == "eth_chainId" else ["0x1111"]

            # First chain_id query fetches from RPC
            cid1 = rpc.chain_id()
            self.assertEqual(cid1, 31337)
            self.assertEqual(mock_call.call_count, 1)

            # Second chain_id query hits cache without RPC call
            cid2 = rpc.chain_id()
            self.assertEqual(cid2, 31337)
            self.assertEqual(mock_call.call_count, 1)

            # New instance with identical RPC URL reuses static chain_id cache
            rpc2 = EthRpc("http://mock-chain-test.internal:8545")
            self.assertEqual(rpc2.chain_id(), 31337)
            self.assertEqual(mock_call.call_count, 1)

            # Accounts caching
            accs1 = rpc.accounts()
            self.assertEqual(accs1, ["0x1111"])
            self.assertEqual(mock_call.call_count, 2)

            accs2 = rpc.accounts(use_cache=True)
            self.assertEqual(accs2, ["0x1111"])
            self.assertEqual(mock_call.call_count, 2)

    def test_eth_rpc_async_methods(self):
        rpc = EthRpc("http://mock-chain-async.internal:8545")
        with patch.object(rpc, "rpc") as mock_call:
            mock_call.side_effect = lambda method, params=None: "0x7a69" if method == "eth_chainId" else ["0x2222"]

            async def _run():
                cid = await rpc.async_chain_id()
                accs = await rpc.async_accounts()
                res = await rpc.async_rpc("eth_blockNumber")
                return cid, accs, res

            cid, accs, _res = asyncio.run(_run())
            self.assertEqual(cid, 31337)
            self.assertEqual(accs, ["0x2222"])
            self.assertIsNotNone(_res)

    def test_escrow_client_async_helpers(self):
        async def _run():
            tid = await self.client.async_task_id_for("0x" + "aa" * 20, "0x" + "bb" * 32)
            return tid

        expected_task_id = bytes.fromhex("77" * 32)
        self.mock_rpc.rpc.return_value = "0x" + encode(["bytes32"], [expected_task_id]).hex()
        res = asyncio.run(_run())
        self.assertEqual(res, "0x" + "77" * 32)

    def test_parse_public_key_formats(self):
        valid_hex = "0x03" + "aa" * 32
        parsed1 = parse_public_key(valid_hex)
        self.assertEqual(len(parsed1), 33)
        self.assertEqual(parsed1[0], 3)

        parsed2 = parse_public_key(bytes.fromhex("02" + "bb" * 32))
        self.assertEqual(len(parsed2), 33)
        self.assertEqual(parsed2[0], 2)

        with self.assertRaises(ValueError):
            parse_public_key("0x03" + "aa" * 31)  # Invalid length (32 bytes)

    def test_compile_buyer_policy_with_manifest_public_keys(self):
        import tempfile
        from pathlib import Path

        import yaml

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            man_path = tmp / "manifest.yaml"
            tpl_path = tmp / "template.yaml"

            man_path.write_text(yaml.dump({
                "chain_id": 31337,
                "escrow_address": "0x" + "11" * 20,
                "token_address": "0x" + "22" * 20,
                "category": 42,
                "max_amount": 1_000_000,
                "public_keys": {
                    "registry_key": "0x03292900f425be9a0ef0c3f74f22792243f047a7cb4b7fc864fb5b98c21b519ae5",
                    "buyer_acceptance_key": "0x02933a9f1f375230c8721c5840697173ac2eadeae7c77a651a6bcfcb7656ff6d57",
                },
            }))
            tpl_path.write_text(yaml.dump({
                "version": 1,
                "rule": {"all": ["accepted"]},
            }))

            # Notice: no private keys exist in tmp_dir!
            policy, pol_hash = compile_buyer_policy(
                template_path=tpl_path,
                manifest_path=man_path,
            )
            self.assertEqual(
                policy["registry_key"],
                parse_public_key("0x03292900f425be9a0ef0c3f74f22792243f047a7cb4b7fc864fb5b98c21b519ae5"),
            )
            self.assertEqual(
                policy["acceptance_key"],
                parse_public_key("0x02933a9f1f375230c8721c5840697173ac2eadeae7c77a651a6bcfcb7656ff6d57"),
            )
            self.assertTrue(pol_hash.startswith("0x"))

    def test_compile_buyer_policy_with_public_keys_yaml(self):
        import tempfile
        from pathlib import Path

        import yaml

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            man_path = tmp / "manifest.yaml"
            tpl_path = tmp / "template.yaml"
            pub_path = tmp / "public_keys.yaml"

            man_path.write_text(yaml.dump({
                "chain_id": 31337,
                "escrow_address": "0x" + "11" * 20,
                "token_address": "0x" + "22" * 20,
                "category": 42,
                "max_amount": 1_000_000,
                "public_files": {
                    "public_keys": str(pub_path),
                },
            }))
            pub_path.write_text(yaml.dump({
                "registry_key": "0x03292900f425be9a0ef0c3f74f22792243f047a7cb4b7fc864fb5b98c21b519ae5",
                "buyer_acceptance_key": "0x02933a9f1f375230c8721c5840697173ac2eadeae7c77a651a6bcfcb7656ff6d57",
            }))
            tpl_path.write_text(yaml.dump({
                "version": 1,
                "rule": {"all": ["accepted"]},
            }))

            policy, pol_hash = compile_buyer_policy(
                template_path=tpl_path,
                manifest_path=man_path,
            )
            self.assertEqual(
                policy["registry_key"],
                parse_public_key("0x03292900f425be9a0ef0c3f74f22792243f047a7cb4b7fc864fb5b98c21b519ae5"),
            )
            self.assertEqual(
                policy["acceptance_key"],
                parse_public_key("0x02933a9f1f375230c8721c5840697173ac2eadeae7c77a651a6bcfcb7656ff6d57"),
            )
            self.assertTrue(pol_hash.startswith("0x"))

    def test_compile_buyer_policy_derives_max_amount_from_max_budget_usd(self):
        import tempfile
        from pathlib import Path

        import yaml

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            man_path = tmp / "manifest.yaml"
            tpl_path = tmp / "template.yaml"

            man_path.write_text(yaml.dump({
                "chain_id": 31337,
                "escrow_address": "0x" + "11" * 20,
                "token_address": "0x" + "22" * 20,
                "category": 42,
                "max_budget_usd": 2.50,
                "public_keys": {
                    "registry_key": "0x03292900f425be9a0ef0c3f74f22792243f047a7cb4b7fc864fb5b98c21b519ae5",
                    "buyer_acceptance_key": "0x02933a9f1f375230c8721c5840697173ac2eadeae7c77a651a6bcfcb7656ff6d57",
                },
            }))
            tpl_path.write_text(yaml.dump({"version": 1}))

            policy, _ = compile_buyer_policy(
                template_path=tpl_path,
                manifest_path=man_path,
            )
            self.assertEqual(policy["rule"]["all"][1], {"amount_at_most": 2_500_000})

    def test_compile_buyer_policy_fails_loudly_when_keys_missing(self):
        import tempfile
        from pathlib import Path

        import yaml

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            man_path = tmp / "manifest.yaml"
            tpl_path = tmp / "template.yaml"

            man_path.write_text(yaml.dump({
                "chain_id": 31337,
                "escrow_address": "0x" + "11" * 20,
                "token_address": "0x" + "22" * 20,
                # No public_keys or private_keys
            }))
            tpl_path.write_text(yaml.dump({"version": 1}))

            with self.assertRaises(FileNotFoundError) as ctx:
                compile_buyer_policy(
                    template_path=tpl_path,
                    manifest_path=man_path,
                )
            self.assertIn("registry_key' missing", str(ctx.exception))

    def test_ensure_policy_hash_fails_loudly_on_missing_config(self):
        from pathlib import Path
        with self.assertRaises(FileNotFoundError):
            ensure_policy_hash(
                escrow_address="0x" + "11" * 20,
                token_address="0x" + "22" * 20,
                chain_id=31337,
                max_amount=1_000_000,
                manifest_path=Path("/nonexistent/manifest.yaml"),
            )


if __name__ == "__main__":
    unittest.main()
