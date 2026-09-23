#!/usr/bin/env python3
"""Fresh local chain: instantiate policy, fund/accept, prove, settle, verify balances.
Uses only Anvil's public unlocked test accounts. Never connects to a public network.
"""
import argparse, json, os, pathlib, subprocess, time, urllib.request, socket
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

out = ROOT/'artifacts'/f'escrow-{time.time_ns()}'
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
    CUSTOMER,AGENT,RELAYER=accounts[:3]
    run('cargo','build','-p','warrant-host','--release','--locked')
    run('forge','build',cwd=ROOT/'contracts')
    image=run('target/release/warrant-host','image-id')
    token=deploy('test/PolicyExecutionVault.t.sol:TestToken')
    ENV.update(WARRANT_TOKEN=token, WARRANT_IMAGE_ID=image)
    run('forge','script','script/Deploy.s.sol:Deploy','--broadcast','--slow','--unlocked','--sender',CUSTOMER,'--rpc-url',URL,cwd=ROOT/'contracts')
    deployment=json.loads((ROOT/'contracts/broadcast/Deploy.s.sol/31337/run-latest.json').read_text())
    addresses={t['contractName']:t['contractAddress'] for t in deployment['transactions'] if t['transactionType']=='CREATE'}
    verifier=addresses['RiscZeroGroth16Verifier']
    escrow=addresses['TaskEscrow']
    (out/'deployment.json').write_text(json.dumps(deployment,indent=2))
    now=int(rpc('eth_getBlockByNumber',['latest',False])['timestamp'],16)
    parameters=json.loads((ROOT/'templates/example-parameters.json').read_text())
    parameters['policy']['scope']={'chain_id':31337,'vault':list(bytes.fromhex(escrow[2:])), 'token':list(bytes.fromhex(token[2:]))}
    parameters['policy']['valid_after']=now
    parameters['policy']['valid_until']=now+86400
    (out/'parameters.json').write_text(json.dumps(parameters,indent=2))
    policy=run('target/release/warrant-policy','instantiate','templates/accepted-contractor-v1.json',str(out/'parameters.json'),str(out/'policy.json'))
    assert run('target/release/warrant-policy','hash',str(out/'policy.json')) == policy
    amount=100000000
    send(CUSTOMER,token,'mint(address,uint256)',CUSTOMER,amount)
    send(CUSTOMER,token,'approve(address,uint256)',escrow,amount)
    salt='0x'+'01'*32
    task=call(escrow,'taskIdFor(address,bytes32)(bytes32)',CUSTOMER,salt)
    send(CUSTOMER,escrow,'offer(bytes32,address,bytes32,uint64,uint64,uint64,uint64)',salt,AGENT,policy,1,amount,now+3600,now+86400)
    send(AGENT,escrow,'accept(bytes32)',task)
    assert int(call(escrow,'totalReserved()(uint256)').split()[0])==amount
    if options.deploy_only:
        print('Deployment script, funding and acceptance passed:', out, flush=True)
        raise SystemExit(0)
    fixture=run('cargo','run','--quiet','--locked','-p','warrant-policy','--example','escrow_fixture','--',str(out/'policy.json'),task,AGENT,str(amount))
    (out/'request.json').write_text(fixture)
    print('Funded and accepted task. Generating real proof; artifacts:',out,flush=True)
    run('target/release/warrant-host','prove',str(out/'request.json'),str(out/'request.receipt'))
    print('Wrapping real proof for EVM verification',flush=True)
    run('target/release/warrant-host','wrap',str(out/'request.receipt'),str(out/'request-groth16.receipt'))
    run('target/release/warrant-host','export-evm',str(out/'request-groth16.receipt'),str(out/'evm.json'))
    proof=json.loads((out/'evm.json').read_text())
    call(verifier,'verify(bytes,bytes32,bytes32)',proof['seal'],proof['imageId'],proof['journalDigest'])
    settled=send(RELAYER,escrow,'settle(bytes,bytes)',proof['seal'],proof['journal'])
    assert int(call(token,'balanceOf(address)(uint256)',AGENT).split()[0])==amount
    assert int(call(token,'balanceOf(address)(uint256)',escrow).split()[0])==0
    assert int(call(escrow,'totalReserved()(uint256)').split()[0])==0
    try: call(escrow,'settle(bytes,bytes)',proof['seal'],proof['journal'])
    except RuntimeError: pass
    else: raise AssertionError('Replay unexpectedly accepted')
    summary=dict(chainId=31337,token=token,verifier=verifier,escrow=escrow,imageId=image,taskId=task,policyHash=policy,
        recipient=AGENT,amount=amount,transaction=settled['transactionHash'],gasUsed=settled['gasUsed'],realProof=True,replayRejected=True)
    (out/'result.json').write_text(json.dumps(summary,indent=2)+'\n')
    print('Real proof settled; recipient paid; replay rejected. Result:',out/'result.json',flush=True)
finally:
    node.terminate()
    try: node.wait(timeout=10)
    except subprocess.TimeoutExpired: node.kill(); node.wait()
    log.close()
