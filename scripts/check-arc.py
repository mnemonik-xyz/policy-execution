#!/usr/bin/env python3
"""Read-only Arc Testnet eth_call: real verifier, tamper rejection and escrow constructor.
Does not broadcast, fund an account or claim a persistent contract deployment.
"""
import argparse, json, pathlib, subprocess
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('proof',type=pathlib.Path)
parser.add_argument('--rpc',default='https://rpc.testnet.arc.io')
args=parser.parse_args()
root=pathlib.Path(__file__).resolve().parents[1]
def run(*command):
    return subprocess.check_output(command,cwd=root/'contracts',text=True).strip()
assert run('cast','chain-id','--rpc-url',args.rpc)=='5042002'
proof=json.loads(args.proof.read_text())
run('forge','build')
bytecode=run('forge','inspect','ArcProbe','bytecode')
encoded=run('cast','abi-encode','constructor(address,bytes,bytes32,bytes32)',
    '0x3600000000000000000000000000000000000000',proof['seal'],proof['imageId'],proof['journalDigest'])
request=json.dumps({'data':bytecode+encoded[2:],'gas':hex(15000000)})
result=json.loads(run('cast','rpc','--rpc-url',args.rpc,'eth_call',request,'latest'))
assert result=='0x'+'00'*31+'01',result
print('Arc Testnet read-only simulation passed: real proof, tamper rejection, USDC interface and escrow construction.')
