#!/usr/bin/env python3
"""Fresh local chain: fund a purchase order, vendor accepts, then settle invoices two
ways: a sub-threshold invoice by signature from the buyer-run signer (sub-second),
and the same flow by real proof (prove, wrap for EVM, settle). Rejects replay.
Uses only Anvil's public test accounts and keys. Never connects to a public network.
"""
import argparse, json, os, pathlib, socket, subprocess, time, urllib.request
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--deploy-only", action="store_true", help="Exercise deployment, funding and acceptance without proving")
parser.add_argument("--signed-only", action="store_true", help="Stop after the signed settlement; skip proving")
options = parser.parse_args()
ROOT = pathlib.Path(__file__).resolve().parents[1]
os.chdir(ROOT)
ENV = dict(os.environ, RISC0_BUILD_LOCKED='1', RAYON_NUM_THREADS='4', DOCKER_DEFAULT_PLATFORM='linux/amd64')
def run(*args, cwd=ROOT):
    p = subprocess.run(args, cwd=cwd, env=ENV, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError(f'{args[0]} failed: {p.stderr}\n{p.stdout}')
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

out = ROOT/'artifacts'/f'invoice-{time.time_ns()}'
out.mkdir(parents=True)
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
    image=run('target/release/warrant-host','invoice-image-id')
    token=deploy('test/PolicyExecutionVault.t.sol:TestToken')
    ENV.update(WARRANT_TOKEN=token, WARRANT_IMAGE_ID=image, WARRANT_SIGNER=SIGNER, WARRANT_PROOF_THRESHOLD=str(2000*10**6))
    run('forge','script','script/DeployInvoice.s.sol:DeployInvoice','--broadcast','--slow','--unlocked','--sender',CUSTOMER,'--rpc-url',URL,cwd=ROOT/'contracts')
    deployment=json.loads((ROOT/'contracts/broadcast/DeployInvoice.s.sol/31337/run-latest.json').read_text())
    addresses={t['contractName']:t['contractAddress'] for t in deployment['transactions'] if t['transactionType']=='CREATE'}
    verifier=addresses['RiscZeroGroth16Verifier']
    escrow=addresses['InvoiceEscrow']
    (out/'deployment.json').write_text(json.dumps(deployment,indent=2))
    now=int(rpc('eth_getBlockByNumber',['latest',False])['timestamp'],16)
    # The fixture's windows nest inside [base, base + 4000]; start slightly in the past.
    base=now-300
    run('cargo','run','--quiet','--locked','-p','warrant-policy','--example','invoice_fixture','--',
        str(out),'31337',escrow,token,VENDOR,str(base))
    terms=json.loads((out/'terms.json').read_text())
    ceiling, amount = terms['maxTotal'], terms['amount']
    send(CUSTOMER,token,'mint(address,uint256)',CUSTOMER,ceiling)
    send(CUSTOMER,token,'approve(address,uint256)',escrow,ceiling)
    order=call(escrow,'orderIdFor(bytes32,bytes32)(bytes32)',terms['policyHash'],terms['poId'])
    # Up to half the ceiling may settle on the signer's word; the rest needs proofs.
    send(CUSTOMER,escrow,'offer(bytes32,uint64,bytes32,address,uint64,uint64,uint64,uint64)',
         terms['policyHash'],terms['policyVersion'],terms['poId'],VENDOR,ceiling,ceiling//2,now+600,base+3700)
    send(VENDOR,escrow,'accept(bytes32)',order)
    assert number(call(escrow,'totalReserved()(uint256)'))==ceiling
    if options.deploy_only:
        print('Deployment script, order funding and vendor acceptance passed:', out, flush=True)
        raise SystemExit(0)
    # Signer mode: the buyer-run service evaluates natively and signs; settles in one transaction.
    started=time.time()
    run('target/release/warrant-host','invoice-sign',str(signer_key_file),str(out/'input.json'),str(out/'signed.json'))
    signed=json.loads((out/'signed.json').read_text())
    assert int(call(escrow,'signerDigest(bytes)(bytes32)',signed['journal']),16)  # digest computable on chain
    settled_signed=send(RELAYER,escrow,'settleSigned(bytes,bytes)',signed['journal'],signed['signature'])
    signed_seconds=time.time()-started
    assert number(call(token,'balanceOf(address)(uint256)',VENDOR))==amount
    assert number(call(escrow,'remaining(bytes32)(uint64)',order))==ceiling-amount
    try: call(escrow,'settleSigned(bytes,bytes)',signed['journal'],signed['signature'])
    except RuntimeError: pass
    else: raise AssertionError('Signed replay unexpectedly accepted')
    print(f'Signed settlement done in {signed_seconds:.2f}s (evaluate, sign, submit). Artifacts:',out,flush=True)
    if options.signed_only:
        (out/'result.json').write_text(json.dumps(dict(chainId=31337,token=token,escrow=escrow,signer=SIGNER,orderId=order,
            amount=amount,signedTransaction=settled_signed['transactionHash'],signedGasUsed=settled_signed['gasUsed'],
            signedSeconds=round(signed_seconds,2),realProof=False),indent=2)+'\n')
        raise SystemExit(0)
    # Proof mode for a second invoice against the same order; the first is already consumed.
    run('cargo','run','--quiet','--locked','-p','warrant-policy','--example','invoice_fixture','--',
        str(out/'proof'),'31337',escrow,token,VENDOR,str(base),'INV-1002')
    print('Generating real invoice proof for a second invoice; artifacts:',out,flush=True)
    run('target/release/warrant-host','invoice-prove',str(out/'proof/input.json'),str(out/'invoice.receipt'))
    print('Wrapping real proof for EVM verification',flush=True)
    run('target/release/warrant-host','wrap',str(out/'invoice.receipt'),str(out/'invoice-groth16.receipt'))
    run('target/release/warrant-host','export-evm',str(out/'invoice-groth16.receipt'),str(out/'evm.json'))
    proof=json.loads((out/'evm.json').read_text())
    assert proof['imageId']==image
    call(verifier,'verify(bytes,bytes32,bytes32)',proof['seal'],proof['imageId'],proof['journalDigest'])
    settled=send(RELAYER,escrow,'settle(bytes,bytes)',proof['seal'],proof['journal'])
    assert number(call(token,'balanceOf(address)(uint256)',VENDOR))==2*amount
    assert number(call(escrow,'remaining(bytes32)(uint64)',order))==ceiling-2*amount
    assert number(call(escrow,'totalReserved()(uint256)'))==ceiling-2*amount
    try: call(escrow,'settle(bytes,bytes)',proof['seal'],proof['journal'])
    except RuntimeError: pass
    else: raise AssertionError('Replay unexpectedly accepted')
    summary=dict(chainId=31337,token=token,verifier=verifier,escrow=escrow,imageId=image,signer=SIGNER,orderId=order,
        policyHash=terms['policyHash'],poId=terms['poId'],recipient=VENDOR,amount=amount,ceiling=ceiling,
        signedTransaction=settled_signed['transactionHash'],signedGasUsed=settled_signed['gasUsed'],signedSeconds=round(signed_seconds,2),
        provenTransaction=settled['transactionHash'],provenGasUsed=settled['gasUsed'],realProof=True,replayRejected=True)
    (out/'result.json').write_text(json.dumps(summary,indent=2)+'\n')
    print('Signed and proven invoices settled; vendor paid twice; replays rejected. Result:',out/'result.json',flush=True)
finally:
    node.terminate()
    try: node.wait(timeout=10)
    except subprocess.TimeoutExpired: node.kill(); node.wait()
    log.close()
