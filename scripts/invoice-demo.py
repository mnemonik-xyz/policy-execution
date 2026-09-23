#!/usr/bin/env python3
"""Fresh local chain: fund a purchase order, vendor accepts, prove an invoice with the
evidence checker, wrap for EVM, settle, then reject replay and over-ceiling payment.
Uses only Anvil's public unlocked test accounts. Never connects to a public network.
"""
import argparse, json, os, pathlib, socket, subprocess, time, urllib.request
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--deploy-only", action="store_true", help="Exercise deployment, funding and acceptance without proving")
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
    run('cargo','build','-p','warrant-host','--release','--locked')
    run('forge','build',cwd=ROOT/'contracts')
    image=run('target/release/warrant-host','invoice-image-id')
    token=deploy('test/PolicyExecutionVault.t.sol:TestToken')
    ENV.update(WARRANT_TOKEN=token, WARRANT_IMAGE_ID=image)
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
    send(CUSTOMER,escrow,'offer(bytes32,uint64,bytes32,address,uint64,uint64,uint64)',
         terms['policyHash'],terms['policyVersion'],terms['poId'],VENDOR,ceiling,now+600,base+3700)
    send(VENDOR,escrow,'accept(bytes32)',order)
    assert number(call(escrow,'totalReserved()(uint256)'))==ceiling
    if options.deploy_only:
        print('Deployment script, order funding and vendor acceptance passed:', out, flush=True)
        raise SystemExit(0)
    print('Funded and accepted purchase order. Generating real invoice proof; artifacts:',out,flush=True)
    run('target/release/warrant-host','invoice-prove',str(out/'input.json'),str(out/'invoice.receipt'))
    print('Wrapping real proof for EVM verification',flush=True)
    run('target/release/warrant-host','wrap',str(out/'invoice.receipt'),str(out/'invoice-groth16.receipt'))
    run('target/release/warrant-host','export-evm',str(out/'invoice-groth16.receipt'),str(out/'evm.json'))
    proof=json.loads((out/'evm.json').read_text())
    assert proof['imageId']==image
    call(verifier,'verify(bytes,bytes32,bytes32)',proof['seal'],proof['imageId'],proof['journalDigest'])
    settled=send(RELAYER,escrow,'settle(bytes,bytes)',proof['seal'],proof['journal'])
    assert number(call(token,'balanceOf(address)(uint256)',VENDOR))==amount
    assert number(call(escrow,'remaining(bytes32)(uint64)',order))==ceiling-amount
    assert number(call(escrow,'totalReserved()(uint256)'))==ceiling-amount
    try: call(escrow,'settle(bytes,bytes)',proof['seal'],proof['journal'])
    except RuntimeError: pass
    else: raise AssertionError('Replay unexpectedly accepted')
    summary=dict(chainId=31337,token=token,verifier=verifier,escrow=escrow,imageId=image,orderId=order,
        policyHash=terms['policyHash'],poId=terms['poId'],taskId=terms['taskId'],recipient=VENDOR,amount=amount,
        ceiling=ceiling,transaction=settled['transactionHash'],gasUsed=settled['gasUsed'],realProof=True,replayRejected=True)
    (out/'result.json').write_text(json.dumps(summary,indent=2)+'\n')
    print('Real invoice proof settled; vendor paid; replay rejected. Result:',out/'result.json',flush=True)
finally:
    node.terminate()
    try: node.wait(timeout=10)
    except subprocess.TimeoutExpired: node.kill(); node.wait()
    log.close()
