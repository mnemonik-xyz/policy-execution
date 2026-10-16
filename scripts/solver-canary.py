#!/usr/bin/env python3
"""Drive one solver-bounty settlement on a public chain, one reviewed step at a time.

Every step that spends is a dry run unless --execute, --confirm-chain and --confirm-amount are all given.
Keys stay in a Foundry keystore or a Ledger. This script never reads, prints or stores a private key.
The RPC URL comes from the WARRANT_CANARY_RPC variable. It is not kept in arguments, state or errors.
All state lives in one directory. A transaction intent is saved before broadcast and is never retried.
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
NATIVE, HOST = ROOT / "target/debug/warrant-solver", ROOT / "target/release/warrant-host"
MAINNET_IDS = {5042}
ARC_USDC = "0x3600000000000000000000000000000000000000"
HARD_CAP = 10_000_000  # 10 USDC in six-decimal units. Raising it needs a code change and a review.
ROLES = ("deployer", "buyer", "seller", "relayer")
RPC_ENV = "WARRANT_CANARY_RPC"
# The caller's environment reaches every child, including ETH_PASSWORD (Foundry reads it as the path of a password
# file) and FOUNDRY_KEYSTORE_DIR. RAYON_NUM_THREADS defaults to 4 to stop proving from swapping.
ENV = dict(os.environ, RISC0_BUILD_LOCKED="1", RAYON_NUM_THREADS=os.environ.get("RAYON_NUM_THREADS") or "4",
           DOCKER_DEFAULT_PLATFORM="linux/amd64")
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
HASH32 = re.compile(r"0x[0-9a-fA-F]{64}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# Evidence layout and manifest, see solver-bounty/canary/README.md and manifest.schema.json.
CANARY_ROOT = ROOT / "solver-bounty/canary"
EVIDENCE_FOLDERS = {31337: "local", 5042: "mainnet"}
COMPILER = {"solc": "0.8.28", "evm_version": "cancun", "optimizer_runs": 200}  # contracts/foundry.toml
DEPENDENCIES = {"openzeppelin_contracts": "5.6.1",  # contracts/package.json
                "risc0_ethereum_contracts": "v3.0.0",  # contracts/vendor/risc0/PROVENANCE.json
                "risc0_zkvm": "3.0.5"}  # Cargo.lock
BUILDER_IMAGE = "risczero/risc0-guest-builder"
DEFAULT_BUILDER_TAG = "r0.1.88.0"  # DEFAULT_DOCKER_TAG in risc0-build 3.0.5; RISC0_DOCKER_CONTAINER_TAG overrides it
BUILDER = re.compile(r"risczero/risc0-guest-builder:[A-Za-z0-9._-]+|local")
WORKER_REPOSITORY = "ghcr.io/mnemonik-xyz/transcription"
TX_NAMES = ("deploy", "deploy_verifier", "approve", "offer", "accept", "settle", "refund")
ACCEPT_MARGIN = 120  # seconds before acceptBy after which offer and accept refuse, so neither lands too late
TASK_TERMS = "tasks(bytes32)(address,address,bytes32,uint64,uint64,uint64,uint64,uint8)"
TASK_STATES = ("Missing", "Offered", "Accepted", "Paid", "Refunded")  # TaskEscrow.State
REUSED_KEYS = ("escrow", "verifier", "image_id", "code_hashes", "image_builder")
REFUNDED = "This run was refunded. Start a new run with init --reuse-deployment."
BASE_NOTE = "The checker proves a modelled cost, not a cloud bill. Settlement does not start any cloud run."
# The reference wording. The README and the evidence PR body copy this sentence; they do not define their own.
SCRIPTED_CANARY_NOTE = ("Scripted canary: all four roles (deployer, buyer, seller, relayer) are held by the team and "
                        "the quote comes from the heuristic scripts/solver-agent.py. An agent-driven proven deal and "
                        "a separate proving service are the next feature.")
MANIFEST_KEYS = ("source_commit", "source_dirty", "compiler", "dependencies", "chain_id", "token", "escrow",
                 "verifier", "image_id", "image_builder", "code_hashes", "transactions", "addresses", "created_at")


class CanaryError(Exception):
    pass


def rpc_url():
    url = os.environ.get(RPC_ENV, "")
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise CanaryError(f"Set {RPC_ENV} to an https RPC URL, or a loopback URL for a local rehearsal")
    return url


def run(*args, cwd=ROOT, interactive=False, stdin=None):
    if args[0] in ("cast", "forge"):
        env = dict(ENV, ETH_RPC_URL=rpc_url())
    else:  # Only cast and forge sign, so only they get the password file path.
        env = {key: value for key, value in ENV.items() if key != "ETH_PASSWORD"}
    extra = {"input": stdin} if stdin is not None else {} if interactive else {"stdin": subprocess.DEVNULL}
    try:
        p = subprocess.run([str(a) for a in args], cwd=cwd, env=env, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, **extra)
    except FileNotFoundError as exc:
        raise CanaryError(f"Command not found: {args[0]}. Install it or build it first.") from exc
    if p.returncode:
        text = (p.stderr + p.stdout).replace(os.environ.get(RPC_ENV) or "\0", "<rpc>")
        text = text.replace(os.environ.get("ETH_PASSWORD") or "\0", "<password-file>")
        raise CanaryError(f"{args[0]} {args[1] if len(args) > 1 else ''} failed:\n{text[-1500:]}")
    return p.stdout.strip()


def write_new(path, data):
    with open(path, "x") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")


def save(d, st):
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as stream:
        json.dump(st, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, pathlib.Path(d) / "state.json")


def load(d):
    path = pathlib.Path(d) / "state.json"
    if not path.is_file():
        raise CanaryError("No state.json in --dir; run init first")
    return json.loads(path.read_text())


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def parse_role(spec, mainnet):
    kind, _, value = spec.partition(":")
    if kind == "account" and re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        root = pathlib.Path(os.environ.get("FOUNDRY_KEYSTORE_DIR", "~/.foundry/keystores")).expanduser()
        try:
            return ["--account", value], "0x" + json.loads((root / value).read_text())["address"]
        except (OSError, ValueError, KeyError) as exc:
            raise CanaryError(f"Cannot read the address of keystore {value!r} in {root}") from exc
    if kind == "ledger" and ADDRESS.fullmatch(value):
        return ["--ledger", "--from", value], value
    if kind == "unlocked" and ADDRESS.fullmatch(value) and not mainnet:
        return ["--unlocked", "--from", value], value
    raise CanaryError(f"Bad signer {kind or spec!r}: use account:NAME or ledger:0xADDRESS"
                      + ("" if mainnet else ", or unlocked:0xADDRESS on a local chain"))


def signer(st, role):
    return parse_role(st["roles"][role], st["chain_id"] in MAINNET_IDS)


def call(address, signature, *values, sender=None):
    return run("cast", "call", address, signature, *values, *(["--from", sender] if sender else []))


def uint(text):
    return int(text.split()[0])


def connect(st):
    actual = int(run("cast", "chain-id"))
    if actual != st["chain_id"]:
        raise CanaryError(f"RPC reports chain {actual}, but this run is for chain {st['chain_id']}")


def confirmed(st, args):
    if not args.execute:
        return False
    if args.confirm_chain != st["chain_id"] or args.confirm_amount != st["amount"]:
        raise CanaryError("--execute needs --confirm-chain and --confirm-amount equal to the values in state.json")
    return True


def refuse_retry(st, name):
    if name in st["tx"]:
        raise CanaryError(f"{name} was already attempted: {st['tx'][name]}. "
                          "Inspect the chain before any manual change.")


def step(d, st, name, args, simulate, execute):
    """Simulate first. Broadcast only with --execute. Save the intent first and never retry."""
    refuse_retry(st, name)
    simulate()
    print(f"{name}: dry run passed", flush=True)
    if not confirmed(st, args):
        print("No transaction sent. Add --execute --confirm-chain ID --confirm-amount UNITS to broadcast.")
        return None
    st["tx"][name] = {"intent_at": now_iso()}
    save(d, st)
    result = execute()
    st["tx"][name].update(result)
    save(d, st)
    print(f"{name}: sent {result.get('hash', '')}", flush=True)
    return result


def send_tx(st, role, address, signature, *values):
    flags, _ = signer(st, role)
    receipt = json.loads(run("cast", "send", address, signature, *values, *flags, "--json", interactive=True))
    if int(receipt["status"], 16) != 1:
        raise CanaryError("Transaction reverted: " + receipt["transactionHash"])
    return receipt


def cmd_init(args):
    mainnet = args.chain_id in MAINNET_IDS
    if not 0 < args.amount <= HARD_CAP:
        raise CanaryError(f"--amount must be between 1 and {HARD_CAP} token units")
    if not ADDRESS.fullmatch(args.token):
        raise CanaryError("--token must be an address")
    if not 0 < args.accept_window < args.settle_window:
        raise CanaryError("--settle-window must be longer than --accept-window")
    addresses = {role: parse_role(getattr(args, role), mainnet)[1].lower() for role in ROLES}
    if addresses["buyer"] == addresses["seller"] or (mainnet and len(set(addresses.values())) != len(ROLES)):
        raise CanaryError("Use different signers: four on mainnet, and at least different buyer and seller")
    instance = json.loads(pathlib.Path(args.instance).read_text())
    d = pathlib.Path(args.dir)
    reused, probe = reused_deployment(args) if args.reuse_deployment else ({}, None)
    d.mkdir(parents=True)
    write_new(d / "instance.json", instance)
    if reused:
        write_new(d / "reuse-probe.json", probe)
        if (pathlib.Path(args.reuse_deployment) / "deployment.json").is_file():
            shutil.copyfile(pathlib.Path(args.reuse_deployment) / "deployment.json", d / "deployment.json")
    save(d, dict(version=1, created_at=now_iso(), chain_id=args.chain_id, token=args.token, amount=args.amount,
                 max_cost=args.max_cost, accept_window=args.accept_window, settle_window=args.settle_window,
                 roles={role: getattr(args, role) for role in ROLES}, addresses=addresses, **reused, tx={}))
    print("Created", d, *([f"(reusing the deployment of {reused['reused_from']})"] if reused else []))


def same_hex(a, b):
    return isinstance(a, str) and isinstance(b, str) and a.lower() == b.lower()


def probe_deployment(old, args):
    """Run the read-only probe check-arc-solver.py after on the reused contracts and return its report.

    The probe compares the code on chain with the local build outside the immutable slots, so the check does not
    rest only on the hashes in the old state.json. It also checks the getters and refuses a live reservation.
    """
    with tempfile.TemporaryDirectory() as tmp:
        manifest, out = pathlib.Path(tmp) / "manifest.json", pathlib.Path(tmp) / "report.json"
        keys = {key: old[key] for key in ("escrow", "verifier", "image_id", "code_hashes")}
        manifest.write_text(json.dumps(dict(keys, chain_id=args.chain_id, token=args.token)))
        allow = [] if args.chain_id in MAINNET_IDS else ["--allow-chain", args.chain_id]
        try:
            run(sys.executable, ROOT / "scripts/check-arc-solver.py", "after", manifest, "--out", out, *allow)
        except CanaryError as exc:
            raise CanaryError(f"The reused deployment failed the read-only probe:\n{exc}") from exc
        return json.loads(out.read_text())


def reused_deployment(args):
    """Check an earlier run's contracts on chain again. Return the keys a new run copies and the probe report.

    Creates nothing on disk.
    """
    old_dir = pathlib.Path(args.reuse_deployment)
    old = load(old_dir)
    if any(key not in old for key in REUSED_KEYS) or not isinstance(old["code_hashes"], dict):
        raise CanaryError(f"{old_dir} holds no complete deployment: it needs {', '.join(REUSED_KEYS)}")
    if old["chain_id"] != args.chain_id or not same_hex(old["token"], args.token):
        raise CanaryError(f"{old_dir} was deployed for chain {old['chain_id']} and token {old['token']}, "
                          "not for the chain and token of this run")
    connect({"chain_id": args.chain_id})
    for name in ("escrow", "verifier"):
        if not same_hex(run("cast", "keccak", run("cast", "code", old[name])), old["code_hashes"].get(name)):
            raise CanaryError(f"The {name} code on chain differs from the code hash recorded in {old_dir}")
    if not same_hex(call(old["escrow"], "imageId()(bytes32)"), old["image_id"]):
        raise CanaryError(f"The escrow on chain holds a different image ID than {old_dir} records")
    if not same_hex(call(old["escrow"], "token()(address)"), args.token):
        raise CanaryError("The escrow on chain holds a different token than this run")
    if not same_hex(call(old["escrow"], "verifier()(address)"), old["verifier"]):
        raise CanaryError(f"The escrow on chain uses a different verifier than {old_dir} records")
    probe = probe_deployment(old, args)
    return dict({key: old[key] for key in REUSED_KEYS}, reused_from=old_dir.resolve().name), probe


def cmd_check(args):
    st = load(args.dir)
    connect(st)
    problems, report = [], dict(checked_at=now_iso(), chain_id=st["chain_id"], token=st["token"], balances={})
    if uint(call(st["token"], "decimals()(uint8)")) != 6:
        problems.append("token does not report six decimals")
    if "RISC0_DEV_MODE" in os.environ:
        problems.append("RISC0_DEV_MODE is set; fake receipts are prohibited")
    for role, address in st["addresses"].items():
        gas = int(run("cast", "balance", address))
        usdc = uint(call(st["token"], "balanceOf(address)(uint256)", address))
        report["balances"][role] = dict(address=address, gas_wei=gas, token_units=usdc)
        if gas == 0:
            problems.append(f"{role} has no gas")
    if report["balances"]["buyer"]["token_units"] < st["amount"]:
        problems.append("buyer holds less than the bounty amount")
    report["problems"] = problems
    write_new(pathlib.Path(args.dir) / f"check-{now_iso().replace(':', '')}.json", report)
    print(json.dumps(report, indent=2))
    return 1 if problems else 0


def build():
    run("cargo", "build", "--release", "--locked", "-p", "warrant-host")
    run("cargo", "build", "--locked", "-p", "warrant-solver")


def image_builder():
    """The guest builder that methods/build.rs selects. Recorded at build time, not read back from Docker."""
    if os.environ.get("RISC0_USE_DOCKER") != "1":
        return "local"
    return f"{BUILDER_IMAGE}:{os.environ.get('RISC0_DOCKER_CONTAINER_TAG', DEFAULT_BUILDER_TAG)}"


def cmd_deploy(args):
    d, st = args.dir, load(args.dir)
    connect(st)
    if "escrow" in st:
        raise CanaryError("Already deployed in this state directory")
    if os.environ.get("RISC0_USE_DOCKER") != "1" and not args.allow_local_image_id:
        raise CanaryError("Set RISC0_USE_DOCKER=1 for a reproducible image ID, or pass --allow-local-image-id")
    build()
    image = run(HOST, "solver-image-id")
    ENV.update(WARRANT_TOKEN=st["token"], WARRANT_SOLVER_IMAGE_ID=image)
    flags, sender = signer(st, "deployer")
    base = ["forge", "script", "script/DeploySolver.s.sol:DeploySolver", "--sender", sender]
    def broadcast():
        run(*base, *flags, "--broadcast", "--slow", cwd=ROOT / "contracts", interactive=True)
        return {"hash": "see deployment.json"}

    result = step(d, st, "deploy", args, lambda: run(*base, cwd=ROOT / "contracts"), broadcast)
    if result is None:
        return
    latest = ROOT / f"contracts/broadcast/DeploySolver.s.sol/{st['chain_id']}/run-latest.json"
    broadcast = json.loads(latest.read_text())
    found = {t["contractName"]: t["contractAddress"]
             for t in broadcast["transactions"] if t["transactionType"] == "CREATE"}
    escrow, verifier = found["SolverBountyEscrow"], found["RiscZeroGroth16Verifier"]
    if call(escrow, "token()(address)").lower() != st["token"].lower() or call(escrow, "imageId()(bytes32)") != image:
        raise CanaryError("Deployed escrow does not hold the expected token and image ID")
    hashes = {name: run("cast", "keccak", run("cast", "code", address)) for name, address in
              (("escrow", escrow), ("verifier", verifier))}
    st.update(escrow=escrow, verifier=verifier, image_id=image, code_hashes=hashes, image_builder=image_builder())
    save(d, st)
    write_new(pathlib.Path(d) / "deployment.json", broadcast)
    print(json.dumps(dict(escrow=escrow, verifier=verifier, image_id=image, code_hashes=hashes), indent=2))


def replace(path, data):
    tmp = pathlib.Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def hexbytes(text):
    return list(bytes.fromhex(text[2:]))


def cmd_quote(args):
    d, st = pathlib.Path(args.dir), load(args.dir)
    connect(st)
    if "escrow" not in st:
        raise CanaryError("Deploy first")
    if "offer" in st["tx"]:
        raise CanaryError("The offer was already attempted. Its terms are fixed.")
    build()
    now = int(run("cast", "block", "latest", "--field", "timestamp"))
    seller, buyer = st["addresses"]["seller"], st["addresses"]["buyer"]
    instance = json.loads((d / "instance.json").read_text())
    instance_hash = run(NATIVE, "instance-hash", d / "instance.json")
    rfq = dict(protocol="warrant.solver.v1", type="rfq", instance=instance, instanceHash=instance_hash,
               maxCost=st["max_cost"], amount=st["amount"], seller=seller)
    quote = json.loads(run(sys.executable, ROOT / "scripts/solver-agent.py", stdin=json.dumps(rfq)))
    if (quote["instanceHash"], quote["amount"], quote["seller"].lower(), quote["checkerVersion"]) != (
            instance_hash, st["amount"], seller, 1):
        raise CanaryError("Quote does not match the approved terms")
    accept_by, settle_by = now + st["accept_window"], now + st["settle_window"]
    scope = dict(chain_id=st["chain_id"], vault=hexbytes(st["escrow"]), token=hexbytes(st["token"]))
    policy = dict(version=1, scope=scope, valid_after=now, valid_until=settle_by, checker_version=1,
                  instance_hash=hexbytes(instance_hash), max_cost=st["max_cost"],
                  rule={"all": ["accepted", {"amount_at_most": st["amount"]}, {"recipient_equals": hexbytes(seller)}]})
    for name, value in (("rfq", rfq), ("quote", quote), ("policy", policy)):
        replace(d / f"{name}.json", value)
    salt = "0x" + hashlib.sha256(st["created_at"].encode()).hexdigest()
    st["quote"] = dict(salt=salt, task=call(st["escrow"], "taskIdFor(address,bytes32)(bytes32)", buyer, salt),
                       policy_hash=run(NATIVE, "policy-hash", d / "policy.json"), instance_hash=instance_hash,
                       accept_by=accept_by, settle_by=settle_by, quoted_at=now)
    save(d, st)
    print(json.dumps(st["quote"], indent=2))


OFFER = "offer(bytes32,address,bytes32,uint64,uint64,uint64,uint64)"


def cmd_offer(args):
    d, st = args.dir, load(args.dir)
    connect(st)
    q, escrow, token = st.get("quote"), st.get("escrow"), st["token"]
    if not q:
        raise CanaryError("Run quote first")
    if block_time() + ACCEPT_MARGIN > q["accept_by"]:
        raise CanaryError("The quote is too old to offer. Run quote again.")
    buyer, amount = st["addresses"]["buyer"], str(st["amount"])
    if "approve" in st["tx"] and "hash" not in st["tx"]["approve"]:
        raise CanaryError("approve was started but not confirmed. Inspect the chain.")
    if "approve" not in st["tx"]:  # Approve exactly the bounty, never an unlimited allowance.
        step(d, st, "approve", args,
             lambda: call(token, "approve(address,uint256)(bool)", escrow, amount, sender=buyer),
             lambda: {"hash": send_tx(st, "buyer", token, "approve(address,uint256)", escrow,
                                      amount)["transactionHash"]})
        if "approve" not in st["tx"]:
            print("Dry run only. Execute approve first; offer is simulated after it.")
            return
    terms = [q["salt"], st["addresses"]["seller"], q["policy_hash"], "1", amount,
             str(q["accept_by"]), str(q["settle_by"])]
    step(d, st, "offer", args, lambda: call(escrow, OFFER + "(bytes32)", *terms, sender=buyer),
         lambda: {"hash": send_tx(st, "buyer", escrow, OFFER, *terms)["transactionHash"]})


def block_time():
    return int(run("cast", "block", "latest", "--field", "timestamp"))


def cmd_accept(args):
    d, st = args.dir, load(args.dir)
    if "refund" in st["tx"]:
        raise CanaryError(REFUNDED)
    connect(st)
    q, escrow = st.get("quote"), st.get("escrow")
    if not q or "hash" not in st["tx"].get("offer", {}):
        raise CanaryError("The offer is not confirmed yet")
    if run(NATIVE, "policy-hash", pathlib.Path(d) / "policy.json") != q["policy_hash"]:
        raise CanaryError("policy.json no longer matches the quoted policy hash")
    seller, buyer = st["addresses"]["seller"], st["addresses"]["buyer"]
    onchain = call(escrow, TASK_TERMS, q["task"])
    expected = [buyer, seller, q["policy_hash"], "1", str(st["amount"]), str(q["accept_by"]), str(q["settle_by"]), "1"]
    if [line.split()[0].lower() for line in onchain.splitlines()] != [value.lower() for value in expected]:
        raise CanaryError("The seller rejects the on-chain terms")
    refuse_retry(st, "accept")  # A lost-response retry says so before the margin message can hide it.
    if block_time() + ACCEPT_MARGIN > q["accept_by"]:
        raise CanaryError(f"Less than {ACCEPT_MARGIN} s remain before acceptBy, so accept could land too late. "
                          "Do not accept. Wait until after acceptBy, run refund, then init --reuse-deployment.")
    done = step(d, st, "accept", args, lambda: call(escrow, "accept(bytes32)", q["task"], sender=seller),
                lambda: {"hash": send_tx(st, "seller", escrow, "accept(bytes32)", q["task"])["transactionHash"]})
    if done:
        try:
            call(escrow, "refund(bytes32)", q["task"], sender=buyer)
        except CanaryError:
            st.setdefault("checks", {})["buyer_cancellation_rejected"] = True
            save(d, st)
        else:
            raise CanaryError("Buyer cancellation still works after acceptance. Stop and investigate.")


def cmd_deliver(args):
    d, st = pathlib.Path(args.dir), load(args.dir)
    q = st.get("quote")
    if not q or "hash" not in st["tx"].get("accept", {}):
        raise CanaryError("The seller has not accepted on chain yet")
    if "delivery" in st:
        raise CanaryError("Already delivered in this state directory")
    rfq = json.loads((d / "rfq.json").read_text())
    work = json.loads(run(sys.executable, ROOT / "scripts/solver-agent.py",
                          stdin=json.dumps(dict(rfq, type="work", taskId=q["task"]))))
    if work["taskId"] != q["task"]:
        raise CanaryError("The delivery names the wrong task")
    write_new(d / "delivery.json", work)
    write_new(d / "schedule.json", work["schedule"])
    seller = st["addresses"]["seller"]
    run(NATIVE, "prepare", d / "policy.json", d / "instance.json", d / "schedule.json", q["task"], seller,
        st["amount"], d / "input.json")
    native = json.loads(run(NATIVE, "inspect", d / "input.json"))
    write_new(d / "native.json", native)
    result = native["result"]
    st["delivery"] = dict(result=result, result_hash="0x" + hashlib.sha256(bytes.fromhex(result[2:])).hexdigest(),
                          total_cost=native["authorization"]["total_cost"])
    save(d, st)
    print(json.dumps(st["delivery"], indent=2))


def cmd_prove(args):
    d, st = pathlib.Path(args.dir), load(args.dir)
    if "delivery" not in st:
        raise CanaryError("Run deliver first")
    if "RISC0_DEV_MODE" in os.environ:
        raise CanaryError("Unset RISC0_DEV_MODE; fake receipts are prohibited")
    build()
    (d / "prove.log").write_text(run(HOST, "solver-prove", d / "input.json", d / "solver.receipt") + "\n")
    run(HOST, "wrap", d / "solver.receipt", d / "solver-groth16.receipt")
    run(HOST, "export-evm", d / "solver-groth16.receipt", d / "evm.json")
    proof, native = json.loads((d / "evm.json").read_text()), json.loads((d / "native.json").read_text())
    if proof["journal"] != native["journal"]:
        raise CanaryError("Proven and native journals differ")
    if proof["imageId"].lower() != st["image_id"].lower():
        raise CanaryError("The proof was made by a different guest than the deployed image ID")
    st["proof"] = dict(image_id=proof["imageId"], journal_digest=proof.get("journalDigest"))
    save(d, st)
    print("Real Groth16 proof ready:", d / "evm.json")


SETTLE = "settleWithResult(bytes,bytes,bytes)"


def cmd_settle(args):
    d, st = pathlib.Path(args.dir), load(args.dir)
    if "refund" in st["tx"]:
        raise CanaryError(REFUNDED)
    connect(st)
    if "proof" not in st:
        raise CanaryError("Run prove first")
    proof = json.loads((d / "evm.json").read_text())
    escrow, token, q = st["escrow"], st["token"], st["quote"]
    relayer, seller = st["addresses"]["relayer"], st["addresses"]["seller"]
    result = st["delivery"]["result"]
    changed = result[:-2] + ("01" if result[-2:] != "01" else "02")
    try:
        call(escrow, SETTLE, proof["seal"], proof["journal"], changed, sender=relayer)
    except CanaryError:
        pass
    else:
        raise CanaryError("A substituted result passed simulation. Stop and investigate.")
    before = uint(call(token, "balanceOf(address)(uint256)", seller))

    def execute():
        receipt = send_tx(st, "relayer", escrow, SETTLE, proof["seal"], proof["journal"], result)
        write_new(d / "settlement.json", receipt)
        return {"hash": receipt["transactionHash"], "block": receipt["blockNumber"]}

    done = step(str(d), st, "settle", args,
                lambda: call(escrow, SETTLE, proof["seal"], proof["journal"], result, sender=relayer), execute)
    if not done:
        return
    receipt = json.loads((d / "settlement.json").read_text())
    if uint(call(token, "balanceOf(address)(uint256)", seller)) - before != st["amount"]:
        raise CanaryError("The seller balance did not rise by the bounty amount")
    if uint(call(escrow, "totalReserved()(uint256)")) != 0:
        raise CanaryError("The reservation was not consumed")
    topic = run("cast", "keccak", "ResultPublished(bytes32,bytes32,uint64,bytes)")
    logs = [x for x in receipt["logs"] if x["address"].lower() == escrow.lower() and x["topics"][0] == topic]
    if len(logs) != 1 or logs[0]["topics"][1] != q["task"] or logs[0]["topics"][2] != st["delivery"]["result_hash"]:
        raise CanaryError("The result publication event is missing or wrong")
    if run("cast", "abi-decode", "f()(uint64,bytes)", logs[0]["data"]).splitlines()[-1] != result:
        raise CanaryError("Published bytes differ from the delivered schedule")
    try:
        call(escrow, SETTLE, proof["seal"], proof["journal"], result, sender=relayer)
    except CanaryError:
        st.setdefault("checks", {}).update(result_substitution_rejected=True, replay_rejected=True,
                                           seller_paid=True, result_published=True)
        save(str(d), st)
    else:
        raise CanaryError("Replay passed simulation. Stop and investigate.")


def task_terms(st):
    """The on-chain task tuple as first words: customer, recipient, policy hash, version, amount, ..., state."""
    return [line.split()[0] for line in call(st["escrow"], TASK_TERMS, st["quote"]["task"]).splitlines()]


def task_state(st):
    return int(task_terms(st)[-1])


def refund_refusal(st, state, now, sender):
    """The rules of TaskEscrow.refund, so that a refund that would revert is refused before the dry run."""
    q = st["quote"]
    if state == 1 and now <= q["accept_by"] and sender != st["addresses"]["buyer"]:
        return "Before acceptBy only the buyer can cancel an unaccepted offer. Use --role buyer"
    if state == 2 and now <= q["settle_by"]:
        return "The seller accepted. A refund is possible only after settleBy"
    if state not in (1, 2):
        return f"The task is {TASK_STATES[state] if state < len(TASK_STATES) else state} and cannot be refunded"
    return None


def balances(st):
    return {name: uint(call(st["token"], "balanceOf(address)(uint256)", address))
            for name, address in (("escrow", st["escrow"]), ("buyer", st["addresses"]["buyer"]))}


def expected_buyer_gain(st, receipt, sender):
    """The buyer gain and its tolerance. On Arc, USDC is the gas token, so a buyer who sends pays the fee in it."""
    if sender != st["addresses"]["buyer"] or not same_hex(st["token"], ARC_USDC):
        return st["amount"], 0
    try:
        fee = int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)  # native units, 18 decimals
    except (KeyError, TypeError, ValueError) as exc:
        raise CanaryError("Refund post-check: the receipt lacks a hex gasUsed or effectiveGasPrice") from exc
    return st["amount"] - fee // 10**12, 1  # token units, 6 decimals; 1 unit for rounding


def check_refund(st, receipt, before, sender):
    escrow, task, amount = st["escrow"], st["quote"]["task"], st["amount"]
    if task_state(st) != 4:
        raise CanaryError("Refund post-check: the task state is not Refunded")
    topic = run("cast", "keccak", "Refunded(bytes32)")
    logs = [x for x in receipt.get("logs") or [] if isinstance(x, dict) and same_hex(x.get("address"), escrow)
            and len(x.get("topics") or []) > 1 and same_hex(x["topics"][0], topic) and same_hex(x["topics"][1], task)]
    if len(logs) != 1:
        raise CanaryError("Refund post-check: the receipt does not hold exactly one Refunded event for this task")
    after = balances(st)
    if before["escrow"] - after["escrow"] != amount:
        raise CanaryError("Refund post-check: the escrow balance did not fall by the bounty amount")
    expected, tolerance = expected_buyer_gain(st, receipt, sender)
    if abs(after["buyer"] - before["buyer"] - expected) > tolerance:
        raise CanaryError(f"Refund post-check: the buyer balance did not rise by {expected} token units")


def cmd_refund(args):
    d, st = pathlib.Path(args.dir), load(args.dir)
    if not st.get("quote") or "hash" not in st["tx"].get("offer", {}):
        raise CanaryError("The offer is not confirmed, so there is nothing to refund")
    refuse_retry(st, "refund")
    connect(st)
    escrow, task, sender = st["escrow"], st["quote"]["task"], st["addresses"][args.role]
    terms = task_terms(st)
    if not same_hex(terms[0], st["addresses"]["buyer"]) or terms[4] != str(st["amount"]):
        raise CanaryError("The on-chain task names another buyer or amount than state.json. Stop and investigate.")
    refusal = refund_refusal(st, int(terms[-1]), block_time(), sender)
    if refusal:
        raise CanaryError(refusal)
    before, sent = balances(st), {}

    def execute():
        sent["receipt"] = send_tx(st, args.role, escrow, "refund(bytes32)", task)
        return {"hash": sent["receipt"]["transactionHash"], "block": sent["receipt"]["blockNumber"]}

    if not step(d, st, "refund", args, lambda: call(escrow, "refund(bytes32)", task, sender=sender), execute):
        return
    write_new(d / "refund.json", sent["receipt"])  # After step() saved the hash, so a failed write cannot lose it.
    check_refund(st, sent["receipt"], before, sender)
    st.setdefault("checks", {})["refunded"] = True
    save(d, st)
    print("refund: post-checks passed (state Refunded, one Refunded event, escrow and buyer balances)", flush=True)


def evidence_folder(d, chain_id):
    """A run directory sits directly under canary/local for 31337 or canary/mainnet for 5042. Nothing else."""
    folder = EVIDENCE_FOLDERS.get(chain_id)
    if folder is None:
        raise CanaryError(f"No evidence folder for chain {chain_id}: only 31337 (local) and 5042 (mainnet) are allowed")
    expected = (CANARY_ROOT / folder).resolve()
    if d.resolve().parent != expected:
        raise CanaryError(f"Evidence for chain {chain_id} must be a run directory directly under {expected}")


def deploy_hashes(d):
    path = d / "deployment.json"
    if not path.is_file():
        return {}
    created = {t["contractName"]: t.get("hash") for t in json.loads(path.read_text())["transactions"]
               if t["transactionType"] == "CREATE"}
    pairs = (("deploy", created.get("SolverBountyEscrow")), ("deploy_verifier", created.get("RiscZeroGroth16Verifier")))
    return {name: value for name, value in pairs if value}


def manifest_problems(m):
    problems = [f"missing {key}" for key in MANIFEST_KEYS if key not in m]
    patterns = {"source_commit": re.compile(r"[0-9a-f]{40}"), "token": ADDRESS, "escrow": ADDRESS,
                "verifier": ADDRESS, "image_id": HASH32, "image_builder": BUILDER}
    for key, pattern in patterns.items():
        if key in m and not (isinstance(m[key], str) and pattern.fullmatch(m[key])):
            problems.append(f"bad {key}")
    if not isinstance(m.get("source_dirty"), bool):
        problems.append("source_dirty must be a boolean")
    if m.get("chain_id") not in EVIDENCE_FOLDERS:
        problems.append("chain_id must be 31337 or 5042")
    if m.get("chain_id") == 5042 and not str(m.get("image_builder", "")).startswith(BUILDER_IMAGE + ":"):
        problems.append("a mainnet manifest must record a Docker builder image")
    if m.get("compiler") != COMPILER:
        problems.append("compiler must be " + json.dumps(COMPILER))
    if m.get("dependencies", {}).get("openzeppelin_contracts") != DEPENDENCIES["openzeppelin_contracts"]:
        problems.append("dependencies.openzeppelin_contracts must be " + DEPENDENCIES["openzeppelin_contracts"])
    return problems + manifest_table_problems(m)


def manifest_table_problems(m):
    problems = []
    for name in ("escrow", "verifier"):
        if not HASH32.fullmatch(str(m.get("code_hashes", {}).get(name, ""))):
            problems.append(f"bad code_hashes.{name}")
    for name, value in m.get("transactions", {}).items():
        if name not in TX_NAMES or not HASH32.fullmatch(str(value)):
            problems.append(f"bad transactions.{name}")
    for role in ROLES:
        if not ADDRESS.fullmatch(str(m.get("addresses", {}).get(role, ""))):
            problems.append(f"bad addresses.{role}")
    worker = m.get("worker_image")
    if worker is not None and (set(worker) != {"repository", "digest"} or not DIGEST.fullmatch(str(worker["digest"]))
                               or not str(worker["repository"]).startswith("ghcr.io/")):
        problems.append("bad worker_image")
    try:
        datetime.datetime.fromisoformat(str(m.get("created_at")))
    except ValueError:
        problems.append("bad created_at")
    return problems


def check_manifest(m):
    """A hand-written subset of manifest.schema.json: required keys, patterns, fixed values and the chain rule."""
    problems = manifest_problems(m)
    if problems:
        raise CanaryError("manifest.json is invalid: " + "; ".join(problems))


def build_manifest(d, st, worker_digest):
    for key in ("escrow", "verifier", "image_id", "code_hashes"):
        if key not in st:
            raise CanaryError("Deploy first: the manifest needs the deployed addresses, code hashes and image ID")
    if "image_builder" not in st:
        raise CanaryError("state.json has no image_builder; this run was deployed by an older script version")
    sent = {name: tx["hash"] for name, tx in st["tx"].items() if HASH32.fullmatch(str(tx.get("hash", "")))}
    commit, dirty = run("git", "rev-parse", "HEAD"), bool(run("git", "status", "--porcelain"))
    manifest = {"source_commit": commit, "source_dirty": dirty, "compiler": dict(COMPILER),
                "dependencies": dict(DEPENDENCIES), "chain_id": st["chain_id"], "token": st["token"],
                "escrow": st["escrow"], "verifier": st["verifier"], "image_id": st["image_id"],
                "image_builder": st["image_builder"], "code_hashes": st["code_hashes"],
                "transactions": {**deploy_hashes(d), **sent}, "addresses": st["addresses"], "created_at": now_iso()}
    if worker_digest:
        manifest["worker_image"] = {"repository": WORKER_REPOSITORY, "digest": worker_digest}
    check_manifest(manifest)
    return manifest


def cmd_evidence(args):
    d, st = pathlib.Path(args.dir), load(args.dir)
    evidence_folder(d, st["chain_id"])
    if args.worker_digest and not DIGEST.fullmatch(args.worker_digest):
        raise CanaryError("--worker-digest must be sha256: followed by 64 lowercase hex digits")
    for name in ("manifest.json", "result.json"):
        if (d / name).exists():
            raise CanaryError(f"{name} already exists. A run directory is never edited after evidence. Start a new run")
    manifest = build_manifest(d, st, args.worker_digest)
    summary = dict(chain_id=st["chain_id"], escrow=st.get("escrow"), verifier=st.get("verifier"), token=st["token"],
                   image_id=st.get("image_id"), code_hashes=st.get("code_hashes"), amount=st["amount"],
                   addresses=st["addresses"], quote=st.get("quote"), delivery=st.get("delivery"),
                   transactions=st["tx"], checks=st.get("checks", {}), realProof="proof" in st, mockVerifier=False,
                   reused_from=st.get("reused_from"),
                   note=BASE_NOTE + (" " + SCRIPTED_CANARY_NOTE if st["chain_id"] in MAINNET_IDS else ""))
    write_new(d / "manifest.json", manifest)
    write_new(d / "result.json", summary)
    print(json.dumps(summary, indent=2))


def parser():
    top = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(dest="command", required=True)

    def add(name, handler, spends=False):
        p = sub.add_parser(name)
        p.add_argument("--dir", required=True, help="State directory")
        if spends:
            p.add_argument("--execute", action="store_true", help="Broadcast. Without it the step is a dry run")
            p.add_argument("--confirm-chain", type=int)
            p.add_argument("--confirm-amount", type=int)
        p.set_defaults(handler=handler)
        return p

    init = add("init", cmd_init)
    init.add_argument("--chain-id", type=int, default=5042)
    init.add_argument("--token", default=ARC_USDC)
    init.add_argument("--amount", type=int, required=True, help=f"Token units, at most {HARD_CAP}")
    init.add_argument("--max-cost", type=int, required=True, help="Checker cost ceiling in instance units")
    init.add_argument("--instance", required=True)
    init.add_argument("--accept-window", type=int, default=3600)
    init.add_argument("--settle-window", type=int, default=14400)
    for role in ROLES:
        init.add_argument(f"--{role}", required=True, help="account:NAME, ledger:0xADDRESS or unlocked:0xADDRESS")
    init.add_argument("--reuse-deployment", metavar="DIR",
                      help="Earlier run directory whose deployed contracts this run uses again. No redeploy")
    add("check", cmd_check)
    deploy = add("deploy", cmd_deploy, spends=True)
    deploy.add_argument("--allow-local-image-id", action="store_true")
    for name, handler in (("quote", cmd_quote), ("deliver", cmd_deliver), ("prove", cmd_prove)):
        add(name, handler)
    add("evidence", cmd_evidence).add_argument(
        "--worker-digest", help="GHCR digest of the worker image, sha256:<64 hex>, read at deploy time of the GPU run")
    for name, handler in (("offer", cmd_offer), ("accept", cmd_accept), ("settle", cmd_settle)):
        add(name, handler, spends=True)
    add("refund", cmd_refund, spends=True).add_argument(
        "--role", choices=("buyer", "seller", "relayer"), default="buyer",
        help="Sender of the refund (default buyer). Funds always return to the buyer")
    return top


def main():
    args = parser().parse_args()
    try:
        sys.exit(args.handler(args) or 0)
    except (CanaryError, FileExistsError) as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
