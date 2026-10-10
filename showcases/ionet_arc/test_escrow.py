"""Unit tests for EscrowClient and EthRpc using eth-abi and eth-account."""
import unittest
from unittest.mock import MagicMock, patch

from eth_abi import encode
from eth_account import Account
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from showcases.ionet_arc.escrow import (
    EscrowClient,
    encode_journal,
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


if __name__ == "__main__":
    unittest.main()
