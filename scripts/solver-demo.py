#!/usr/bin/env python3
"""Buyer, seller and relayer complete a solver bounty on a private local Anvil.

Default: generate and settle a real Groth16 proof (requires RISC Zero and Docker).
--native-only: show negotiation and native checking, with no chain or proof claim.
--mock-settlement: test the complete actor flow with a clearly labelled mock verifier.
All funds and the bundled workload are synthetic. No public RPC or private keys.
"""
import argparse
import hashlib
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV = dict(os.environ, RISC0_BUILD_LOCKED="1", RAYON_NUM_THREADS="4",
           DOCKER_DEFAULT_PLATFORM="linux/amd64")
NATIVE = ROOT / "target/debug/warrant-solver"
HOST = ROOT / "target/release/warrant-host"


def run(*args, cwd=ROOT, stdin=None):
    p = subprocess.run([str(a) for a in args], cwd=cwd, env=ENV, input=stdin,
                       text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError(f"{args[0]} failed:\n{p.stderr}\n{p.stdout}")
    return p.stdout.strip()


def write(path, data):
    with path.open("x") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--native-only", action="store_true")
    mode.add_argument("--mock-settlement", action="store_true")
    parser.add_argument("--instance", type=pathlib.Path, default=ROOT / "solver-bounty/instance.json")
    parser.add_argument("--max-cost", type=int, default=14)
    parser.add_argument("--amount", type=int, default=10_000_000, help="Test token base units")
    args = parser.parse_args()
    if "RISC0_DEV_MODE" in os.environ:
        raise RuntimeError("Unset RISC0_DEV_MODE; fake receipts are prohibited")
    run("cargo", "build", "--locked", "-p", "warrant-solver")
    out = ROOT / "artifacts" / f"solver-{time.time_ns()}"
    out.mkdir(parents=True)
    instance = json.loads(args.instance.read_text())
    write(out / "instance.json", instance)
    instance_hash = run(NATIVE, "instance-hash", out / "instance.json")
    node = log = None
    summary = dict(realProof=False, chainSettlement=False, mockVerifier=args.mock_settlement,
                   bundledSyntheticWorkload=args.instance.resolve() == (ROOT / "solver-bounty/instance.json").resolve())
    try:
        if args.native_only:
            customer, seller, relayer = ["0x" + byte * 20 for byte in ["11", "22", "33"]]
            escrow, token = "0x" + "44" * 20, "0x" + "55" * 20
            task, now = "0x" + "66" * 32, 1000
        else:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            url = f"http://127.0.0.1:{port}"

            def rpc(method, params=None):
                request = urllib.request.Request(url, json.dumps(dict(jsonrpc="2.0", id=1, method=method, params=params or [])).encode(), {"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=15) as response:
                    value = json.load(response)
                if "error" in value:
                    raise RuntimeError(value["error"])
                return value["result"]

            def send(sender, address, signature, *values):
                receipt = json.loads(run("cast", "send", address, signature, *values,
                                         "--from", sender, "--unlocked", "--rpc-url", url, "--json"))
                if int(receipt["status"], 16) != 1:
                    raise RuntimeError("Transaction reverted")
                return receipt

            def call(address, signature, *values):
                return run("cast", "call", address, signature, *values, "--rpc-url", url)

            def deploy(name, *values):
                command = ["forge", "create", name, "--broadcast", "--unlocked", "--from", customer, "--rpc-url", url, "--json"]
                if values:
                    command += ["--constructor-args", *values]
                return json.loads(run(*command, cwd=ROOT / "contracts"))["deployedTo"]

            log = (out / "anvil.log").open("w")
            node = subprocess.Popen(["anvil", "--host", "127.0.0.1", "--port", str(port), "--chain-id", "31337", "--silent"], stdout=log, stderr=log, env=ENV)
            for _ in range(100):
                try:
                    customer, seller, relayer = rpc("eth_accounts")[:3]
                    break
                except Exception:
                    if node.poll() is not None:
                        raise RuntimeError("Anvil stopped")
                    time.sleep(.1)
            else:
                raise RuntimeError("Anvil did not start")
            run("forge", "build", cwd=ROOT / "contracts")
            token = deploy("test/PolicyExecutionVault.t.sol:TestToken")
            if args.mock_settlement:
                print("MOCK VERIFIER: settlement integration only; no cryptographic proof", flush=True)
                verifier = deploy("test/PolicyExecutionVault.t.sol:JournalVerifier")
                image = "0x" + "00" * 31 + "01"
                escrow = deploy("src/SolverBountyEscrow.sol:SolverBountyEscrow", token, verifier, image)
            else:
                run("cargo", "build", "--release", "--locked", "-p", "warrant-host")
                image = run(HOST, "solver-image-id")
                ENV.update(WARRANT_TOKEN=token, WARRANT_SOLVER_IMAGE_ID=image)
                run("forge", "script", "script/DeploySolver.s.sol:DeploySolver", "--broadcast", "--slow", "--unlocked", "--sender", customer, "--rpc-url", url, cwd=ROOT / "contracts")
                deployment = json.loads((ROOT / "contracts/broadcast/DeploySolver.s.sol/31337/run-latest.json").read_text())
                addresses = {t["contractName"]: t["contractAddress"] for t in deployment["transactions"] if t["transactionType"] == "CREATE"}
                verifier, escrow = addresses["RiscZeroGroth16Verifier"], addresses["SolverBountyEscrow"]
                write(out / "deployment.json", deployment)
            now = int(rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
            salt = "0x" + "01" * 32
            task = call(escrow, "taskIdFor(address,bytes32)(bytes32)", customer, salt)

        rfq = dict(protocol="warrant.solver.v1", type="rfq", instance=instance,
                   instanceHash=instance_hash, maxCost=args.max_cost, amount=args.amount, seller=seller)
        quote = json.loads(run(sys.executable, ROOT / "scripts/solver-agent.py", stdin=json.dumps(rfq)))
        if quote["instanceHash"] != instance_hash or quote["amount"] != args.amount or quote["seller"] != seller or quote["checkerVersion"] != 1:
            raise RuntimeError("Quote does not match approved terms")
        write(out / "rfq.json", rfq)
        write(out / "quote.json", quote)
        scope = dict(chain_id=31337, vault=list(bytes.fromhex(escrow[2:])), token=list(bytes.fromhex(token[2:])))
        policy = dict(version=1, scope=scope, valid_after=now, valid_until=now + 86400,
                      checker_version=1, instance_hash=list(bytes.fromhex(instance_hash[2:])), max_cost=args.max_cost,
                      rule={"all": ["accepted", {"amount_at_most": args.amount}, {"recipient_equals": list(bytes.fromhex(seller[2:]))}]})
        write(out / "policy.json", policy)
        policy_hash = run(NATIVE, "policy-hash", out / "policy.json")
        if not args.native_only:
            send(customer, token, "mint(address,uint256)", customer, args.amount)
            send(customer, token, "approve(address,uint256)", escrow, args.amount)
            send(customer, escrow, "offer(bytes32,address,bytes32,uint64,uint64,uint64,uint64)", salt, seller, policy_hash, 1, args.amount, now + 3600, now + 86400)
            # Seller checks the on-chain offer before its own acceptance transaction.
            offered = call(escrow, "tasks(bytes32)(address,address,bytes32,uint64,uint64,uint64,uint64,uint8)", task).splitlines()
            expected = [customer, seller, policy_hash, "1", str(args.amount), str(now + 3600), str(now + 86400), "1"]
            if [line.split()[0].lower() for line in offered] != [value.lower() for value in expected]:
                raise RuntimeError("Seller rejected on-chain terms")
            send(seller, escrow, "accept(bytes32)", task)
            try:
                run("cast", "call", escrow, "refund(bytes32)", task, "--from", customer, "--rpc-url", url)
            except RuntimeError:
                summary["buyerCancellationRejected"] = True
            else:
                raise AssertionError("Buyer cancellation succeeded after acceptance")
            print("Funds reserved; seller accepted. Buyer sends no further transactions.", flush=True)

        delivery = json.loads(run(sys.executable, ROOT / "scripts/solver-agent.py", stdin=json.dumps(dict(rfq, type="work", taskId=task))))
        if delivery["taskId"] != task:
            raise RuntimeError("Wrong delivery task")
        write(out / "delivery.json", delivery)
        write(out / "schedule.json", delivery["schedule"])
        run(NATIVE, "prepare", out / "policy.json", out / "instance.json", out / "schedule.json", task, seller, args.amount, out / "input.json")
        native = json.loads(run(NATIVE, "inspect", out / "input.json"))
        write(out / "native.json", native)
        result = native["result"]
        summary.update(taskId=task, policyHash=policy_hash, computedCost=native["authorization"]["total_cost"], resultHash="0x" + hashlib.sha256(bytes.fromhex(result[2:])).hexdigest())
        if args.native_only:
            print("Native schedule and policy checks passed. No proof or settlement.", flush=True)
        else:
            if args.mock_settlement:
                # Test harness authority only. Never substitute this for a production verifier.
                send(relayer, verifier, "approve(bytes)", native["journal"])
                proof = dict(seal="0xabcd", journal=native["journal"], imageId=image)
            else:
                print("Generating real solver proof", flush=True)
                proof_log = run(HOST, "solver-prove", out / "input.json", out / "solver.receipt")
                (out / "prove.log").write_text(proof_log + "\n")
                run(HOST, "wrap", out / "solver.receipt", out / "solver-groth16.receipt")
                run(HOST, "export-evm", out / "solver-groth16.receipt", out / "evm.json")
                proof = json.loads((out / "evm.json").read_text())
                if proof["journal"] != native["journal"]:
                    raise AssertionError("Proven/native journal mismatch")
                summary["realProof"] = True
            changed = result[:-2] + ("01" if result[-2:] != "01" else "02")
            try:
                call(escrow, "settleWithResult(bytes,bytes,bytes)", proof["seal"], proof["journal"], changed)
            except RuntimeError:
                summary["resultSubstitutionRejected"] = True
            else:
                raise AssertionError("Substituted result accepted")
            paid = send(relayer, escrow, "settleWithResult(bytes,bytes,bytes)", proof["seal"], proof["journal"], result)
            if int(call(token, "balanceOf(address)(uint256)", seller).split()[0]) != args.amount:
                raise AssertionError("Seller was not paid")
            if int(call(escrow, "totalReserved()(uint256)").split()[0]) != 0:
                raise AssertionError("Reservation was not consumed")
            event = run("cast", "keccak", "ResultPublished(bytes32,bytes32,uint64,bytes)")
            logs = [log for log in paid["logs"] if log["address"].lower() == escrow.lower() and log["topics"][0] == event]
            if len(logs) != 1 or logs[0]["topics"][1] != task or logs[0]["topics"][2] != summary["resultHash"]:
                raise AssertionError("Missing result publication")
            decoded = run("cast", "abi-decode", "f()(uint64,bytes)", logs[0]["data"]).splitlines()
            if decoded[-1] != result:
                raise AssertionError("Published bytes differ from delivered schedule")
            try:
                call(escrow, "settleWithResult(bytes,bytes,bytes)", proof["seal"], proof["journal"], result)
            except RuntimeError:
                summary["replayRejected"] = True
            else:
                raise AssertionError("Replay accepted")
            summary.update(chainSettlement=True, chainId=31337, escrow=escrow, token=token, verifier=verifier,
                           imageId=image, amount=args.amount, seller=seller, customer=customer,
                           transaction=paid["transactionHash"], resultPublished=True, buyerOffline=True)
            write(out / "settlement.json", paid)
            print("Seller paid; exact result published; substitution and replay rejected.", flush=True)
        write(out / "result.json", summary)
        print(json.dumps(summary, indent=2))
        print("Artifacts:", out)
    finally:
        if node:
            node.terminate()
            try:
                node.wait(timeout=10)
            except subprocess.TimeoutExpired:
                node.kill()
                node.wait()
        if log:
            log.close()


if __name__ == "__main__":
    main()
