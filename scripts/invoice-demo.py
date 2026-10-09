#!/usr/bin/env python3
"""Fresh local chain: fund a purchase order naming a signer, vendor accepts, then
settle invoices three ways: a sub-threshold invoice by signature from the buyer-run
signing service (which reads the order from the chain and signs in well under a
second), an undecided invoice by the buyer's own approval, and a third by real
proof (prove, wrap for EVM, settle). Rejects replay and smuggled inputs.
Uses only Anvil's public test accounts and keys. Never connects to a public network.
"""
import argparse, hashlib, json, os, pathlib, platform, re, shutil, socket, subprocess, sys, time, urllib.request
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=pathlib.Path, help="New directory under artifacts for this run")
parser.add_argument("--proof-only", action="store_true", help="Deploy strict escrow and settle only by real proof")
parser.add_argument("--deploy-only", action="store_true", help="Exercise deployment, funding and acceptance without proving")
parser.add_argument("--signed-only", action="store_true", help="Stop after the signed settlement; skip proving")
parser.add_argument("--ledger", action="store_true", help="With --signed-only: book the settlements with connectors/ledger and check the beancount output")
options = parser.parse_args()
if options.proof_only and (options.signed_only or options.ledger):
    parser.error("--proof-only cannot use --signed-only or --ledger")
if options.ledger and not options.signed_only:
    parser.error("--ledger requires --signed-only")
# Hosts without the zkVM toolchain build the host with RISC0_SKIP_BUILD=1 and get a
# zero image ID, which the deployment refuses. The signed and approved paths never
# verify a proof, so they may run against a stand-in image ID; proving may not.
DEMO_IMAGE_ID = os.environ.get("WARRANT_DEMO_IMAGE_ID")
if DEMO_IMAGE_ID and not options.signed_only:
    parser.error("WARRANT_DEMO_IMAGE_ID is only allowed with --signed-only")
ROOT = pathlib.Path(__file__).resolve().parents[1]
os.chdir(ROOT)
ENV = dict(os.environ, RISC0_BUILD_LOCKED='1', RAYON_NUM_THREADS='4', DOCKER_DEFAULT_PLATFORM='linux/amd64')
def run(*args, cwd=ROOT, expect=0):
    p = subprocess.run(args, cwd=cwd, env=ENV, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != expect:
        raise RuntimeError(f'{args[0]} exited {p.returncode}, expected {expect}: {p.stderr}\n{p.stdout}')
    return p.stdout.strip()
def rpc(method, params=[]):
    req = urllib.request.Request(URL, json.dumps(dict(jsonrpc='2.0', id=1, method=method, params=params)).encode(), {'Content-Type':'application/json'})
    response = json.load(urllib.request.urlopen(req, timeout=10))
    if 'error' in response: raise RuntimeError(response['error'])
    return response['result']
def send(sender, address, signature, *args):
    result = json.loads(run('cast','send',address,signature,*map(str,args),'--from',sender,'--unlocked','--rpc-url',URL,'--json'))
    assert int(result['status'],16) == 1, result
    return result
def call(address, signature, *args):
    return run('cast','call',address,signature,*map(str,args),'--rpc-url',URL)
def deploy(name, *args):
    command = ['forge','create',name,'--broadcast','--unlocked','--from',CUSTOMER,'--rpc-url',URL,'--json']
    if args: command += ['--constructor-args',*map(str,args)]
    return json.loads(run(*command,cwd=ROOT/'contracts'))['deployedTo']
def number(text):
    return int(text.split()[0])

def ledger_demo(out, escrow):
    """Books the two settlements with the ledger connector and checks the result."""
    run('cargo','build','-p','warrant-ids','--release','--locked')
    for name, request in (('invoice-1001.xml', out/'request.json'), ('invoice-1003.xml', out/'ask/request.json')):
        (out/name).write_bytes(bytes(json.loads(request.read_text())['document']))
    host_tool, db_path, books_path = ROOT/'target/release/warrant-ids', out/'ledger.sqlite', out/'books.beancount'
    (out/'ledger.toml').write_text(
        f'[chain]\nrpc_url = "{URL}"\nchain_id = 31337\nescrow = "{escrow}"\ncustomer = "{CUSTOMER}"\n'
        f'from_block = 0\nconfirmations = 0\n'
        f'[tools]\nwarrant_ids = "{host_tool}"\n[store]\npath = "{db_path}"\n'
        f'[beancount]\npath = "{books_path}"\n[vendors]\n"{VENDOR.lower()}" = "Vendor"\n')
    ledger=lambda *a: run(sys.executable,'-m','warrant_ledger','--config',str(out/'ledger.toml'),*a,cwd=ROOT/'connectors/ledger')
    ledger('intake',str(out/'invoice-1001.xml'),str(out/'invoice-1003.xml'))
    ledger('sync')
    ledger('export-beancount')
    kinds=sorted(i['kind'] for i in json.loads(ledger('report','--json')))
    assert kinds==['buyer approval: the checker did not run on chain'], kinds
    books=(out/'books.beancount').read_text()
    assert books.count('Expenses:Warrant:Vendor  ')==2 and 'match: "matched"' in books, books
    if shutil.which('bean-check'):
        ENV['PYTHONPATH']=str(ROOT/'connectors/ledger')  # the plugin lives in the connector package
        run('bean-check',str(out/'books.beancount'))
        checked='bean-check passed'
    else:
        checked='bean-check not installed; skipped'
    print(f'Ledger: both settlements booked against their invoices; {checked}. File: {books_path}',flush=True)

out = (options.output.resolve() if options.output else ROOT/'artifacts'/f'invoice-{time.time_ns()}')
if not out.is_relative_to(ROOT/'artifacts'):
    parser.error('--output must be under artifacts/')
out.mkdir(parents=True, exist_ok=False)
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
URL=f'http://127.0.0.1:{port}'
log=open(out/'anvil.log','w')
node=subprocess.Popen(['anvil','--host','127.0.0.1','--port',str(port),'--chain-id','31337','--silent'],stdout=log,stderr=log)
try:
    for _ in range(100):
        try:
            accounts=rpc('eth_accounts'); break
        except Exception:
            if node.poll() is not None: raise RuntimeError('Anvil stopped; inspect '+str(out/'anvil.log'))
            time.sleep(.1)
    else: raise RuntimeError('Anvil did not start')
    CUSTOMER,VENDOR,RELAYER=accounts[:3]
    # Anvil's fourth public test key stands in for the buyer-run signing service.
    SIGNER=accounts[3]
    SIGNER_KEY='0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6'
    signer_key_file=out/'signer.key'
    signer_key_file.write_text(SIGNER_KEY+'\n')
    run('cargo','build','-p','warrant-host','--release','--locked')
    run('forge','build',cwd=ROOT/'contracts')
    image=DEMO_IMAGE_ID or run('target/release/warrant-host','invoice-image-id')
    token=deploy('test/PolicyExecutionVault.t.sol:TestToken')
    ENV.update(WARRANT_TOKEN=token, WARRANT_IMAGE_ID=image, WARRANT_PROOF_ONLY=str(options.proof_only).lower())
    run('forge','script','script/DeployInvoice.s.sol:DeployInvoice','--broadcast','--slow','--unlocked','--sender',CUSTOMER,'--rpc-url',URL,cwd=ROOT/'contracts')
    deployment=json.loads((ROOT/'contracts/broadcast/DeployInvoice.s.sol/31337/run-latest.json').read_text())
    addresses={t['contractName']:t['contractAddress'] for t in deployment['transactions'] if t['transactionType']=='CREATE'}
    verifier=addresses['RiscZeroGroth16Verifier']
    if options.proof_only:
        factory=addresses['ProofInvoiceFactory']
        topic=run('cast','keccak','EscrowCreated(address,bytes32)')
        created=[event for receipt in deployment['receipts'] for event in receipt['logs']
                 if event['address'].lower()==factory.lower() and event['topics'][0]==topic]
        assert len(created)==1
        escrow='0x'+created[0]['topics'][1][-40:]
    else:
        escrow=addresses['InvoiceEscrow']
    assert call(escrow,'imageId()(bytes32)').lower()==image.lower()
    assert call(escrow,'verifier()(address)').lower()==verifier.lower()
    assert call(escrow,'token()(address)').lower()==token.lower()
    if options.proof_only:
        assert call(escrow,'proofOnly()(bool)')=='true'
        assert call(escrow,'replayRegistry()(address)').lower()==factory.lower()
        assert call(factory,'isEscrow(address)(bool)',escrow)=='true'
        assert call(factory,'token()(address)').lower()==token.lower()
        assert call(factory,'verifier()(address)').lower()==verifier.lower()
    # Simulate each approved constructor and compare its entire returned runtime
    # with deployed code, including immutable values. These constructors do not
    # depend on their own address, caller, timestamp, or mutable chain state.
    # This is a local read-back test, not a public deployment attestation.
    control_source=(ROOT/'contracts/vendor/risc0/contracts/src/groth16/ControlID.sol').read_text()
    control={name: '0x'+value for name,value in re.findall(
        r'bytes32 public constant (\w+) = hex"([0-9a-f]{64})";',control_source)}
    contracts=[('RiscZeroGroth16Verifier',verifier,'f(bytes32,bytes32)',
                [control['CONTROL_ROOT'],control['BN254_CONTROL_ID']])]
    if options.proof_only:
        contracts.extend([
            ('ProofInvoiceFactory',factory,'f(address,address)',[token,verifier]),
            ('ProofInvoiceEscrow',escrow,'f(address,address,bytes32,address)',[token,verifier,image,factory])])
    else:
        contracts.append(('InvoiceEscrow',escrow,'f(address,address,bytes32)',[token,verifier,image]))
    readback={}
    for name,address,signature,args in contracts:
        artifact=json.loads((ROOT/f'contracts/out/{name}.sol/{name}.json').read_text())
        encoded=run('cast','abi-encode',signature,*args)
        expected=rpc('eth_call',[{'from':CUSTOMER,'data':artifact['bytecode']['object']+encoded[2:]},'latest'])
        actual=rpc('eth_getCode',[address,'latest'])
        assert len(expected)>2 and actual.lower()==expected.lower(), f'{name} runtime mismatch'
        readback[name]=dict(address=address,constructorArgs=args,runtimeCode=actual)
    (out/'deployment-readback.json').write_text(json.dumps(readback,indent=2)+'\n')
    (out/'deployment.json').write_text(json.dumps(deployment,indent=2))
    (out/'verifier-code.hex').write_text(rpc('eth_getCode',[verifier,'latest'])+'\n')
    now=int(rpc('eth_getBlockByNumber',['latest',False])['timestamp'],16)
    # The fixture's windows nest inside [base, base + 4000]; start slightly in the past.
    base=now-300
    run('cargo','run','--quiet','--locked','-p','warrant-policy','--example','invoice_fixture','--',
        str(out),'31337',escrow,token,VENDOR,CUSTOMER,str(base))
    terms=json.loads((out/'terms.json').read_text())
    ceiling, amount = terms['maxTotal'], terms['amount']
    send(CUSTOMER,token,'mint(address,uint256)',CUSTOMER,ceiling)
    send(CUSTOMER,token,'approve(address,uint256)',escrow,ceiling)
    order=call(escrow,'orderIdFor(address,bytes32,bytes32)(bytes32)',CUSTOMER,terms['policyHash'],terms['poId'])
    # The buyer names the signer for this order: up to half the ceiling may settle on
    # its word, and only invoices under 2000 USDC; the rest needs proofs or approval.
    threshold=2000*10**6
    offer_terms=(f"({terms['policyHash']},{terms['policyVersion']},{terms['poId']},{VENDOR},{ceiling},"
                 f"{SIGNER},{ceiling//2},{threshold},{now+600},{base+3700})")
    if options.proof_only:
        offer_terms=(f"({terms['policyHash']},{terms['policyVersion']},{terms['poId']},{VENDOR},{ceiling},"
                     f"0x{'0'*40},0,0,{now+600},{base+3700})")
    send(CUSTOMER,escrow,'offer((bytes32,uint64,bytes32,address,uint64,address,uint64,uint64,uint64,uint64))',offer_terms)
    send(VENDOR,escrow,'accept(bytes32)',order)
    assert number(call(escrow,'totalReserved()(uint256)'))==ceiling
    if options.deploy_only:
        print('Deployment script, order funding and vendor acceptance passed:', out, flush=True)
        raise SystemExit(0)
    if not options.proof_only:
        # Signer mode: the buyer-run service holds the policy and key, reads the order from
        # the chain, evaluates natively and signs; the relayer settles in one transaction.
        sign=lambda request,output,expect=0: run('target/release/warrant-host','invoice-sign',str(signer_key_file),
            str(out/'policy.json'),URL,str(request),str(output),expect=expect)
        started=time.time()
        sign(out/'request.json',out/'signed.json')
        signed=json.loads((out/'signed.json').read_text())
        assert signed['orderId']==order and signed['amount']==amount
        assert int(call(escrow,'signerDigest(bytes)(bytes32)',signed['journal']),16)  # digest computable on chain
        settled_signed=send(RELAYER,escrow,'settleSigned(bytes,bytes)',signed['journal'],signed['signature'])
        signed_seconds=time.time()-started
        assert number(call(token,'balanceOf(address)(uint256)',VENDOR))==amount
        assert number(call(escrow,'remaining(bytes32)(uint64)',order))==ceiling-amount
        try: call(escrow,'settleSigned(bytes,bytes)',signed['journal'],signed['signature'])
        except RuntimeError: pass
        else: raise AssertionError('Signed replay unexpectedly accepted')
        print(f'Signed settlement done in {signed_seconds:.2f}s (read order, evaluate, sign, submit). Artifacts:',out,flush=True)
        # The agent cannot renumber an invoice or remove its source authentication
        # and still obtain an automatic settlement signature.
        tampered=json.loads((out/'request.json').read_text())
        tampered['document']=list(bytes(tampered['document']).replace(b'INV-1001',b'INV-9001'))
        (out/'tampered.json').write_text(json.dumps(tampered))
        sign(out/'tampered.json',out/'tampered-out.json',expect=1)
        assert not (out/'tampered-out.json').exists()
        missing=json.loads((out/'request.json').read_text())
        missing['invoice_attestation']=None
        missing['invoice_signature']=None
        (out/'missing-attestation.json').write_text(json.dumps(missing))
        sign(out/'missing-attestation.json',out/'missing-attestation-out.json',expect=3)
        assert json.loads((out/'missing-attestation-out.json').read_text())['ask']==['InvoiceAttestationMissing']
        # The service refuses what the agent may not supply: a policy or a spend figure.
        smuggled=json.loads((out/'request.json').read_text()); smuggled['po_spent']=0
        (out/'smuggled.json').write_text(json.dumps(smuggled))
        sign(out/'smuggled.json',out/'smuggled-out.json',expect=1)
        assert not (out/'smuggled-out.json').exists()
        # A key the order does not name signs nothing, even with the right policy.
        other_key_file=out/'other.key'
        other_key_file.write_text('0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba\n')
        run('target/release/warrant-host','invoice-sign',str(other_key_file),str(out/'policy.json'),URL,
            str(out/'request.json'),str(out/'other-out.json'),expect=1)
        assert not (out/'other-out.json').exists()
        # An undecided invoice: a line nobody can label. The service signs nothing and
        # records why; the buyer settles it on their own authority, within the same bounds.
        run('cargo','run','--quiet','--locked','-p','warrant-policy','--example','invoice_fixture','--',
            str(out/'ask'),'31337',escrow,token,VENDOR,CUSTOMER,str(base),'INV-1003','ask')
        sign(out/'ask/request.json',out/'ask.json',expect=3)
        ask=json.loads((out/'ask.json').read_text())
        assert ask['ask']==['UnlabeledLines([1])'] and 'signature' not in ask
        approved=send(CUSTOMER,escrow,'settleApproved(bytes32,bytes32,uint64,bytes32)',ask['orderId'],ask['obligationId'],ask['payable'],ask['documentHash'])
        assert number(call(token,'balanceOf(address)(uint256)',VENDOR))==amount+ask['payable']
        try: send(CUSTOMER,escrow,'settleApproved(bytes32,bytes32,uint64,bytes32)',ask['orderId'],ask['obligationId'],ask['payable'],ask['documentHash'])
        except (RuntimeError,AssertionError): pass
        else: raise AssertionError('Approval replay unexpectedly accepted')
        print('Undecided invoice settled by buyer approval; replay rejected; smuggled inputs and unnamed signers refused.',flush=True)
        if options.ledger:
            ledger_demo(out, escrow)
        if options.signed_only:
            (out/'result.json').write_text(json.dumps(dict(chainId=31337,token=token,escrow=escrow,signer=SIGNER,orderId=order,
                amount=amount,signedTransaction=settled_signed['transactionHash'],signedGasUsed=settled_signed['gasUsed'],
                signedSeconds=round(signed_seconds,2),approvedTransaction=approved['transactionHash'],
                approvedAmount=ask['payable'],realProof=False),indent=2)+'\n')
            raise SystemExit(0)
    # Proof mode for a second invoice against the same order; the first is already consumed.
    run('cargo','run','--quiet','--locked','-p','warrant-policy','--example','invoice_fixture','--',
        str(out/'proof'),'31337',escrow,token,VENDOR,CUSTOMER,str(base),'INV-1002')
    print('Generating real invoice proof for a second invoice; artifacts:',out,flush=True)
    started=time.perf_counter()
    proof_log=run('target/release/warrant-host','invoice-prove',str(out/'proof/input.json'),str(out/'invoice.receipt'))
    prove_seconds=time.perf_counter()-started
    (out/'prove.log').write_text(proof_log+'\n')
    print('Wrapping real proof for EVM verification',flush=True)
    started=time.perf_counter()
    wrap_log=run('target/release/warrant-host','wrap',str(out/'invoice.receipt'),str(out/'invoice-groth16.receipt'))
    wrap_seconds=time.perf_counter()-started
    (out/'wrap.log').write_text(wrap_log+'\n')
    run('target/release/warrant-host','export-evm',str(out/'invoice-groth16.receipt'),str(out/'evm.json'))
    proof=json.loads((out/'evm.json').read_text())
    assert proof['imageId']==image
    call(verifier,'verify(bytes,bytes32,bytes32)',proof['seal'],proof['imageId'],proof['journalDigest'])
    wrong_image='0x'+format(int(image,16)^1,'064x')
    try: call(verifier,'verify(bytes,bytes32,bytes32)',proof['seal'],wrong_image,proof['journalDigest'])
    except RuntimeError: pass
    else: raise AssertionError('Wrong image unexpectedly accepted')
    # Check all 15 words against the cryptographic verifier, so a rejection
    # cannot be explained merely by an escrow domain or accounting constraint.
    for word in range(15):
        altered=bytearray.fromhex(proof['journal'][2:]); altered[word*32+31]^=1
        digest='0x'+hashlib.sha256(altered).hexdigest()
        try: call(verifier,'verify(bytes,bytes32,bytes32)',proof['seal'],image,digest)
        except RuntimeError: pass
        else: raise AssertionError(f'Altered journal word {word} unexpectedly verified')
        try: call(escrow,'settle(bytes,bytes)',proof['seal'],'0x'+altered.hex())
        except RuntimeError: pass
        else: raise AssertionError(f'Altered journal word {word} unexpectedly settled')
    settled=send(RELAYER,escrow,'settle(bytes,bytes)',proof['seal'],proof['journal'])
    paid=amount if options.proof_only else 2*amount+ask['payable']
    assert number(call(token,'balanceOf(address)(uint256)',VENDOR))==paid
    assert number(call(escrow,'remaining(bytes32)(uint64)',order))==ceiling-paid
    assert number(call(escrow,'totalReserved()(uint256)'))==ceiling-paid
    try: call(escrow,'settle(bytes,bytes)',proof['seal'],proof['journal'])
    except RuntimeError: pass
    else: raise AssertionError('Replay unexpectedly accepted')
    summary=dict(chainId=31337, token=token, verifier=verifier, escrow=escrow,
        imageId=image, orderId=order, recipient=VENDOR, amount=amount,
        proofOnly=options.proof_only, realProof=True, replayRejected=True, wrongImageRejected=True, journalTamperingRejected=True,
        tamperedJournalWordsRejected=15, deploymentCodeMatched=True,
        provenTransaction=settled['transactionHash'], provenGasUsed=settled['gasUsed'],
        proveSeconds=round(prove_seconds,2), wrapSeconds=round(wrap_seconds,2))
    if options.proof_only:
        summary.update(replayRegistry=factory)
    if not options.proof_only:
        summary.update(signedTransaction=settled_signed['transactionHash'],
            signedSeconds=round(signed_seconds,2), approvedTransaction=approved['transactionHash'],
            approvedAmount=ask['payable'], invoiceTamperingRejected=True, missingAttestationAsked=True)
    (out/'result.json').write_text(json.dumps(summary,indent=2)+'\n')
    print('Invoice settlement complete; real proof verified; replay rejected. Result:',out/'result.json',flush=True)
finally:
    node.terminate()
    try: node.wait(timeout=10)
    except subprocess.TimeoutExpired: node.kill(); node.wait()
    log.close()
