//! Bitcoin profile primitives: the Taproot HTLC template (spec 8.2), contract
//! identity by re-derivation (S7, spec 8.4), PSBT version 0 decoding, BIP 341
//! sighashes and intent matching (S24, spec 4.2).
//!
//! Own coins are Taproot key-path outputs, so that every signature is a BIP 341
//! signature whose sighash the signer computes itself.

use crate::types::{Lock, TimelockSpec};
use crate::Hash32;
use k256::elliptic_curve::point::{AffineCoordinates, DecompressPoint};
use k256::elliptic_curve::PrimeField;
use k256::{AffinePoint, FieldBytes, ProjectivePoint, Scalar};
use std::fmt;

pub const TEMPLATE_ID: &str = "warrant-htlc-tr-v1";

/// BIP 341 NUMS point `H`, x-only: no one knows its discrete logarithm, so the
/// key path is unspendable.
pub const NUMS_X: Hash32 = [
    0x50, 0x92, 0x9b, 0x74, 0xc1, 0xa0, 0x49, 0x54, 0xb7, 0x8b, 0x4b, 0x60, 0x35, 0xe9, 0x7a, 0x5e,
    0x07, 0x8a, 0x5a, 0x0f, 0x28, 0xec, 0x96, 0xd5, 0x47, 0xbf, 0xee, 0x9a, 0xce, 0x80, 0x3a, 0xc0,
];

const LEAF_VERSION: u8 = 0xc0;
const LOCKTIME_THRESHOLD: u64 = 500_000_000;

const OP_0: u8 = 0x00;
const OP_DROP: u8 = 0x75;
const OP_EQUALVERIFY: u8 = 0x88;
const OP_SIZE: u8 = 0x82;
const OP_SHA256: u8 = 0xa8;
const OP_CHECKSIG: u8 = 0xac;
const OP_CHECKLOCKTIMEVERIFY: u8 = 0xb1;
const OP_CHECKSEQUENCEVERIFY: u8 = 0xb2;
const OP_1: u8 = 0x51;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BtcError {
    MissingKeys,
    InvalidKey,
    BadTimelock,
    Psbt(&'static str),
    Mismatch(String),
}

impl fmt::Display for BtcError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            BtcError::MissingKeys => f.write_str("Bitcoin lock without HTLC keys"),
            BtcError::InvalidKey => f.write_str("not a valid x-only public key"),
            BtcError::BadTimelock => f.write_str("timelock not expressible in the template"),
            BtcError::Psbt(e) => write!(f, "PSBT: {e}"),
            BtcError::Mismatch(e) => write!(f, "transaction does not match the intent: {e}"),
        }
    }
}

impl std::error::Error for BtcError {}

pub fn tagged_hash(tag: &str, msg: &[u8]) -> Hash32 {
    let t = crate::sha256(tag.as_bytes());
    let mut data = Vec::with_capacity(64 + msg.len());
    data.extend_from_slice(&t);
    data.extend_from_slice(&t);
    data.extend_from_slice(msg);
    crate::sha256(&data)
}

fn lift_x(x: &Hash32) -> Option<AffinePoint> {
    Option::from(AffinePoint::decompress(&FieldBytes::from(*x), 0u8.into()))
}

pub fn is_valid_xonly(x: &Hash32) -> bool {
    lift_x(x).is_some()
}

fn push_bytes(script: &mut Vec<u8>, data: &[u8]) {
    assert!(data.len() <= 75, "only direct pushes are used");
    script.push(data.len() as u8);
    script.extend_from_slice(data);
}

/// Minimal CScriptNum push.
fn push_int(script: &mut Vec<u8>, n: u64) {
    if n == 0 {
        script.push(OP_0);
        return;
    }
    if n <= 16 {
        script.push(OP_1 + (n as u8) - 1);
        return;
    }
    let mut bytes = Vec::new();
    let mut v = n;
    while v > 0 {
        bytes.push((v & 0xff) as u8);
        v >>= 8;
    }
    if bytes.last().is_some_and(|b| b & 0x80 != 0) {
        bytes.push(0);
    }
    push_bytes(script, &bytes);
}

/// `OP_SIZE 32 OP_EQUALVERIFY OP_SHA256 <H> OP_EQUALVERIFY <receiver> OP_CHECKSIG`.
pub fn claim_leaf(hashlock: &Hash32, receiver: &Hash32) -> Vec<u8> {
    let mut s = vec![OP_SIZE];
    push_int(&mut s, 32);
    s.extend([OP_EQUALVERIFY, OP_SHA256]);
    push_bytes(&mut s, hashlock);
    s.push(OP_EQUALVERIFY);
    push_bytes(&mut s, receiver);
    s.push(OP_CHECKSIG);
    s
}

/// `<T> OP_CHECKLOCKTIMEVERIFY OP_DROP <refund> OP_CHECKSIG`, or the
/// `OP_CHECKSEQUENCEVERIFY` form for a relative block count.
pub fn refund_leaf(timelock: &TimelockSpec, refund: &Hash32) -> Result<Vec<u8>, BtcError> {
    let mut s = Vec::new();
    match *timelock {
        TimelockSpec::Height(h) if h > 0 && h < LOCKTIME_THRESHOLD => {
            push_int(&mut s, h);
            s.push(OP_CHECKLOCKTIMEVERIFY);
        }
        TimelockSpec::Time(t) if (LOCKTIME_THRESHOLD..=u32::MAX as u64).contains(&t) => {
            push_int(&mut s, t);
            s.push(OP_CHECKLOCKTIMEVERIFY);
        }
        TimelockSpec::RelativeBlocks(n) if n > 0 && n <= 0xffff => {
            push_int(&mut s, n);
            s.push(OP_CHECKSEQUENCEVERIFY);
        }
        _ => return Err(BtcError::BadTimelock),
    }
    s.push(OP_DROP);
    push_bytes(&mut s, refund);
    s.push(OP_CHECKSIG);
    Ok(s)
}

fn compact_size(n: usize, out: &mut Vec<u8>) {
    match n {
        0..=0xfc => out.push(n as u8),
        0xfd..=0xffff => {
            out.push(0xfd);
            out.extend((n as u16).to_le_bytes());
        }
        0x1_0000..=0xffff_ffff => {
            out.push(0xfe);
            out.extend((n as u32).to_le_bytes());
        }
        _ => {
            out.push(0xff);
            out.extend((n as u64).to_le_bytes());
        }
    }
}

pub fn leaf_hash(script: &[u8]) -> Hash32 {
    let mut msg = vec![LEAF_VERSION];
    compact_size(script.len(), &mut msg);
    msg.extend_from_slice(script);
    tagged_hash("TapLeaf", &msg)
}

fn branch_hash(a: &Hash32, b: &Hash32) -> Hash32 {
    let (lo, hi) = if a <= b { (a, b) } else { (b, a) };
    let mut msg = Vec::with_capacity(64);
    msg.extend_from_slice(lo);
    msg.extend_from_slice(hi);
    tagged_hash("TapBranch", &msg)
}

/// BIP 341 output key `Q = P + t·G` with `t = hash_TapTweak(P ‖ root)`, x-only.
pub fn tweak_xonly(internal: &Hash32, merkle_root: &Hash32) -> Option<Hash32> {
    let p = lift_x(internal)?;
    let mut msg = Vec::with_capacity(64);
    msg.extend_from_slice(internal);
    msg.extend_from_slice(merkle_root);
    let t: Option<Scalar> = Scalar::from_repr(tagged_hash("TapTweak", &msg).into()).into();
    let q = (ProjectivePoint::from(p) + ProjectivePoint::GENERATOR * t?).to_affine();
    if q == AffinePoint::IDENTITY {
        return None;
    }
    Some(q.x().into())
}

/// The two leaves of a lock: (claim, refund).
pub fn htlc_leaves(lock: &Lock) -> Result<(Vec<u8>, Vec<u8>), BtcError> {
    let keys = lock.keys.as_ref().ok_or(BtcError::MissingKeys)?;
    // An invalid key makes a leaf unspendable: the claim or the refund would be lost.
    if !is_valid_xonly(&keys.receiver) || !is_valid_xonly(&keys.refund) {
        return Err(BtcError::InvalidKey);
    }
    Ok((claim_leaf(&lock.hashlock, &keys.receiver), refund_leaf(&lock.timelock, &keys.refund)?))
}

/// The `scriptPubKey` of the HTLC output: `OP_1 <32-byte output key>` (BIP 341, BIP 350).
pub fn htlc_script_pubkey(lock: &Lock) -> Result<Vec<u8>, BtcError> {
    let (claim, refund) = htlc_leaves(lock)?;
    let root = branch_hash(&leaf_hash(&claim), &leaf_hash(&refund));
    let q = tweak_xonly(&NUMS_X, &root).ok_or(BtcError::InvalidKey)?;
    let mut spk = vec![OP_1];
    push_bytes(&mut spk, &q);
    Ok(spk)
}

pub fn is_p2tr(script: &[u8]) -> bool {
    script.len() == 34 && script[0] == OP_1 && script[1] == 32
}

// ---------------------------------------------------------------------------
// Transactions and PSBT version 0 (BIP 174)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TxIn {
    /// Internal byte order.
    pub prev_txid: Hash32,
    pub vout: u32,
    pub script_sig: Vec<u8>,
    pub sequence: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TxOut {
    pub value: u64,
    pub script_pubkey: Vec<u8>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Transaction {
    pub version: i32,
    pub inputs: Vec<TxIn>,
    pub outputs: Vec<TxOut>,
    pub lock_time: u32,
}

impl TxOut {
    fn serialize(&self, out: &mut Vec<u8>) {
        out.extend(self.value.to_le_bytes());
        compact_size(self.script_pubkey.len(), out);
        out.extend(&self.script_pubkey);
    }
}

impl Transaction {
    /// Serialization without witness data.
    pub fn serialize(&self) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend(self.version.to_le_bytes());
        compact_size(self.inputs.len(), &mut out);
        for i in &self.inputs {
            out.extend(i.prev_txid);
            out.extend(i.vout.to_le_bytes());
            compact_size(i.script_sig.len(), &mut out);
            out.extend(&i.script_sig);
            out.extend(i.sequence.to_le_bytes());
        }
        compact_size(self.outputs.len(), &mut out);
        for o in &self.outputs {
            o.serialize(&mut out);
        }
        out.extend(self.lock_time.to_le_bytes());
        out
    }

    /// Txid in internal byte order.
    pub fn txid(&self) -> Hash32 {
        crate::sha256(&crate::sha256(&self.serialize()))
    }

    /// Txid as displayed by Bitcoin software (reversed).
    pub fn txid_hex(&self) -> String {
        let mut t = self.txid();
        t.reverse();
        crate::to_hex(&t)
    }
}

struct Reader<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], BtcError> {
        let end = self.pos.checked_add(n).ok_or(BtcError::Psbt("length overflow"))?;
        let slice = self.data.get(self.pos..end).ok_or(BtcError::Psbt("truncated"))?;
        self.pos = end;
        Ok(slice)
    }
    fn u8(&mut self) -> Result<u8, BtcError> {
        Ok(self.take(1)?[0])
    }
    fn u32(&mut self) -> Result<u32, BtcError> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    fn u64(&mut self) -> Result<u64, BtcError> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }
    fn compact(&mut self) -> Result<usize, BtcError> {
        let n = match self.u8()? {
            0xfd => u16::from_le_bytes(self.take(2)?.try_into().unwrap()) as u64,
            0xfe => self.u32()? as u64,
            0xff => self.u64()?,
            b => b as u64,
        };
        usize::try_from(n).map_err(|_| BtcError::Psbt("length overflow"))
    }
    fn var_bytes(&mut self) -> Result<&'a [u8], BtcError> {
        let n = self.compact()?;
        self.take(n)
    }
    fn done(&self) -> bool {
        self.pos == self.data.len()
    }
}

fn parse_txout(r: &mut Reader) -> Result<TxOut, BtcError> {
    Ok(TxOut { value: r.u64()?, script_pubkey: r.var_bytes()?.to_vec() })
}

/// An unsigned transaction without witness data.
pub fn parse_unsigned_tx(bytes: &[u8]) -> Result<Transaction, BtcError> {
    let mut r = Reader { data: bytes, pos: 0 };
    let version = r.u32()? as i32;
    let n_in = r.compact()?;
    if n_in == 0 {
        return Err(BtcError::Psbt("no inputs or witness marker in the unsigned transaction"));
    }
    let mut inputs = Vec::with_capacity(n_in.min(1024));
    for _ in 0..n_in {
        inputs.push(TxIn {
            prev_txid: r.take(32)?.try_into().unwrap(),
            vout: r.u32()?,
            script_sig: r.var_bytes()?.to_vec(),
            sequence: r.u32()?,
        });
    }
    let n_out = r.compact()?;
    let mut outputs = Vec::with_capacity(n_out.min(1024));
    for _ in 0..n_out {
        outputs.push(parse_txout(&mut r)?);
    }
    let lock_time = r.u32()?;
    if !r.done() {
        return Err(BtcError::Psbt("trailing bytes after the transaction"));
    }
    Ok(Transaction { version, inputs, outputs, lock_time })
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct PsbtInput {
    pub witness_utxo: Option<TxOut>,
    pub sighash_type: Option<u32>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Psbt {
    pub tx: Transaction,
    pub inputs: Vec<PsbtInput>,
}

/// Iterate one key-value map; returns (key, value) pairs.
fn parse_map<'a>(r: &mut Reader<'a>) -> Result<Vec<(&'a [u8], &'a [u8])>, BtcError> {
    let mut pairs = Vec::new();
    loop {
        let key = r.var_bytes()?;
        if key.is_empty() {
            return Ok(pairs);
        }
        let value = r.var_bytes()?;
        if pairs.iter().any(|(k, _)| *k == key) {
            return Err(BtcError::Psbt("duplicate key"));
        }
        pairs.push((key, value));
    }
}

/// PSBT version 0. Version 2 (BIP 370) is not accepted in W2.
pub fn parse_psbt(bytes: &[u8]) -> Result<Psbt, BtcError> {
    let mut r = Reader { data: bytes, pos: 0 };
    if r.take(5)? != b"psbt\xff" {
        return Err(BtcError::Psbt("bad magic"));
    }
    let mut tx = None;
    for (key, value) in parse_map(&mut r)? {
        match key[0] {
            0x00 if key.len() == 1 => tx = Some(parse_unsigned_tx(value)?),
            0xfb if key.len() == 1 => {
                if value != [0, 0, 0, 0] {
                    return Err(BtcError::Psbt("only PSBT version 0 is supported"));
                }
            }
            _ => {}
        }
    }
    let tx = tx.ok_or(BtcError::Psbt("missing unsigned transaction"))?;
    if tx.inputs.iter().any(|i| !i.script_sig.is_empty()) {
        return Err(BtcError::Psbt("unsigned transaction carries a scriptSig"));
    }
    let mut inputs = Vec::with_capacity(tx.inputs.len());
    for _ in 0..tx.inputs.len() {
        let mut input = PsbtInput::default();
        for (key, value) in parse_map(&mut r)? {
            match key[0] {
                0x01 if key.len() == 1 => {
                    let mut vr = Reader { data: value, pos: 0 };
                    input.witness_utxo = Some(parse_txout(&mut vr)?);
                    if !vr.done() {
                        return Err(BtcError::Psbt("bad witness UTXO"));
                    }
                }
                0x03 if key.len() == 1 => {
                    let v: [u8; 4] = value.try_into().map_err(|_| BtcError::Psbt("bad sighash type"))?;
                    input.sighash_type = Some(u32::from_le_bytes(v));
                }
                _ => {}
            }
        }
        inputs.push(input);
    }
    for _ in 0..tx.outputs.len() {
        parse_map(&mut r)?;
    }
    if !r.done() {
        return Err(BtcError::Psbt("trailing bytes"));
    }
    Ok(Psbt { tx, inputs })
}

// ---------------------------------------------------------------------------
// BIP 341 signature messages
// ---------------------------------------------------------------------------

/// Only sighash types that commit to every input and output are signed (spec 4.2).
pub fn sighash_type_allowed(t: Option<u32>) -> bool {
    matches!(t, None | Some(0) | Some(1))
}

/// BIP 341 sighash for input `index` with `SIGHASH_DEFAULT` (0x00) or `SIGHASH_ALL`
/// (0x01); `leaf` selects the script path. Every input needs its witness UTXO.
pub fn taproot_sighash(psbt: &Psbt, index: usize, hash_type: u8, leaf: Option<&Hash32>) -> Result<Hash32, BtcError> {
    if hash_type > 1 {
        return Err(BtcError::Psbt("sighash type does not commit to the whole transaction"));
    }
    let tx = &psbt.tx;
    let prevouts: Vec<&TxOut> = psbt
        .inputs
        .iter()
        .map(|i| i.witness_utxo.as_ref().ok_or(BtcError::Psbt("missing witness UTXO")))
        .collect::<Result<_, _>>()?;
    if index >= tx.inputs.len() {
        return Err(BtcError::Psbt("input index"));
    }
    let mut buf = Vec::new();
    for i in &tx.inputs {
        buf.extend(i.prev_txid);
        buf.extend(i.vout.to_le_bytes());
    }
    let sha_prevouts = crate::sha256(&buf);
    buf.clear();
    for p in &prevouts {
        buf.extend(p.value.to_le_bytes());
    }
    let sha_amounts = crate::sha256(&buf);
    buf.clear();
    for p in &prevouts {
        compact_size(p.script_pubkey.len(), &mut buf);
        buf.extend(&p.script_pubkey);
    }
    let sha_spks = crate::sha256(&buf);
    buf.clear();
    for i in &tx.inputs {
        buf.extend(i.sequence.to_le_bytes());
    }
    let sha_sequences = crate::sha256(&buf);
    buf.clear();
    for o in &tx.outputs {
        o.serialize(&mut buf);
    }
    let sha_outputs = crate::sha256(&buf);

    let mut msg = vec![0x00, hash_type];
    msg.extend(tx.version.to_le_bytes());
    msg.extend(tx.lock_time.to_le_bytes());
    msg.extend(sha_prevouts);
    msg.extend(sha_amounts);
    msg.extend(sha_spks);
    msg.extend(sha_sequences);
    msg.extend(sha_outputs);
    msg.push(if leaf.is_some() { 2 } else { 0 });
    msg.extend((index as u32).to_le_bytes());
    if let Some(leaf) = leaf {
        msg.extend(leaf);
        msg.push(0x00);
        msg.extend(u32::MAX.to_le_bytes());
    }
    Ok(tagged_hash("TapSighash", &msg))
}

// ---------------------------------------------------------------------------
// Intent matching (S24)
// ---------------------------------------------------------------------------

/// What the policy signer will sign, bound into the warrant.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BtcBinding {
    pub txid: String,
    pub sighashes: Vec<Hash32>,
    pub sighash_types: Vec<u8>,
}

fn check_sighash_types(psbt: &Psbt) -> Result<Vec<u8>, BtcError> {
    psbt.inputs
        .iter()
        .map(|i| {
            if sighash_type_allowed(i.sighash_type) {
                Ok(i.sighash_type.unwrap_or(0) as u8)
            } else {
                Err(BtcError::Mismatch("sighash type other than DEFAULT or ALL".into()))
            }
        })
        .collect()
}

/// A lock transaction: own Taproot coins in; exactly one HTLC output with the
/// derived script and the leg amount; at most one change output to an own script;
/// nothing else.
pub fn check_lock_psbt(psbt: &Psbt, lock: &Lock, amount: u64, own_scripts: &[Vec<u8>]) -> Result<BtcBinding, BtcError> {
    let types = check_sighash_types(psbt)?;
    let htlc = htlc_script_pubkey(lock)?;
    for input in &psbt.inputs {
        let utxo = input.witness_utxo.as_ref().ok_or(BtcError::Mismatch("input without witness UTXO".into()))?;
        if !is_p2tr(&utxo.script_pubkey) || !own_scripts.contains(&utxo.script_pubkey) {
            return Err(BtcError::Mismatch("input is not an own Taproot coin".into()));
        }
    }
    let mut htlc_outputs = 0;
    let mut change_outputs = 0;
    for out in &psbt.tx.outputs {
        if out.script_pubkey == htlc {
            if out.value != amount {
                return Err(BtcError::Mismatch("HTLC output amount differs from the terms".into()));
            }
            htlc_outputs += 1;
        } else if own_scripts.contains(&out.script_pubkey) {
            change_outputs += 1;
        } else {
            return Err(BtcError::Mismatch("output to a foreign script".into()));
        }
    }
    if htlc_outputs != 1 || change_outputs > 1 {
        return Err(BtcError::Mismatch("expected one HTLC output and at most one change output".into()));
    }
    let sighashes = (0..psbt.inputs.len())
        .map(|i| taproot_sighash(psbt, i, types[i], None))
        .collect::<Result<_, _>>()?;
    Ok(BtcBinding { txid: psbt.tx.txid_hex(), sighashes, sighash_types: types })
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Leaf {
    Claim,
    Refund,
}

/// A claim or refund: one input, the HTLC output; every output to an own script.
/// A refund must satisfy its own timelock in the transaction fields.
pub fn check_spend_psbt(
    psbt: &Psbt,
    lock: &Lock,
    htlc_outpoint: (&Hash32, u32),
    leaf: Leaf,
    own_scripts: &[Vec<u8>],
) -> Result<BtcBinding, BtcError> {
    let types = check_sighash_types(psbt)?;
    let htlc = htlc_script_pubkey(lock)?;
    let tx = &psbt.tx;
    if tx.inputs.len() != 1 {
        return Err(BtcError::Mismatch("a spend has exactly one input".into()));
    }
    let input = &tx.inputs[0];
    if (&input.prev_txid, input.vout) != htlc_outpoint {
        return Err(BtcError::Mismatch("input is not the HTLC output".into()));
    }
    let utxo = psbt.inputs[0].witness_utxo.as_ref().ok_or(BtcError::Mismatch("missing witness UTXO".into()))?;
    if utxo.script_pubkey != htlc {
        return Err(BtcError::Mismatch("witness UTXO is not the HTLC script".into()));
    }
    if tx.outputs.is_empty() || tx.outputs.iter().any(|o| !own_scripts.contains(&o.script_pubkey)) {
        return Err(BtcError::Mismatch("a spend pays own scripts only".into()));
    }
    let (claim, refund) = htlc_leaves(lock)?;
    if leaf == Leaf::Refund {
        let ok = match lock.timelock {
            TimelockSpec::Height(h) => {
                (tx.lock_time as u64) >= h && (tx.lock_time as u64) < LOCKTIME_THRESHOLD && input.sequence != u32::MAX
            }
            TimelockSpec::Time(t) => (tx.lock_time as u64) >= t && input.sequence != u32::MAX,
            TimelockSpec::RelativeBlocks(n) => {
                tx.version >= 2 && input.sequence & (1 << 31) == 0 && input.sequence & (1 << 22) == 0
                    && (input.sequence & 0xffff) as u64 >= n
            }
            TimelockSpec::RelativeSeconds(_) => false,
        };
        if !ok {
            return Err(BtcError::Mismatch("refund does not satisfy its timelock".into()));
        }
    }
    let script = if leaf == Leaf::Claim { claim } else { refund };
    let sighash = taproot_sighash(psbt, 0, types[0], Some(&leaf_hash(&script)))?;
    Ok(BtcBinding { txid: tx.txid_hex(), sighashes: vec![sighash], sighash_types: types })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{HashAlg, HtlcKeys};
    use ::bitcoin as rb;
    use rb::hashes::Hash as _;

    fn xonly(seed: u8) -> Hash32 {
        let secp = rb::secp256k1::Secp256k1::new();
        let sk = rb::secp256k1::SecretKey::from_slice(&[seed; 32]).unwrap();
        rb::secp256k1::Keypair::from_secret_key(&secp, &sk).x_only_public_key().0.serialize()
    }

    pub(crate) fn lock(timelock: TimelockSpec) -> Lock {
        Lock {
            contract: TEMPLATE_ID.into(),
            hash_alg: HashAlg::Sha256,
            hashlock: crate::sha256(&[7; 32]),
            preimage_len: 32,
            timelock,
            swap_id: [9; 32],
            keys: Some(HtlcKeys { receiver: xonly(1), refund: xonly(2) }),
        }
    }

    fn rb_script(bytes: &[u8]) -> rb::ScriptBuf {
        rb::ScriptBuf::from_bytes(bytes.to_vec())
    }

    /// Cross-check the Taproot output against rust-bitcoin's builder.
    #[test]
    fn taproot_output_matches_rust_bitcoin() {
        let secp = rb::secp256k1::Secp256k1::verification_only();
        for tl in [TimelockSpec::Height(850_000), TimelockSpec::Time(1_800_000_000), TimelockSpec::RelativeBlocks(144)] {
            let l = lock(tl);
            let (claim, refund) = htlc_leaves(&l).unwrap();
            let internal = rb::key::XOnlyPublicKey::from_slice(&NUMS_X).unwrap();
            let info = rb::taproot::TaprootBuilder::new()
                .add_leaf(1, rb_script(&claim))
                .unwrap()
                .add_leaf(1, rb_script(&refund))
                .unwrap()
                .finalize(&secp, internal)
                .unwrap();
            let expected = rb::ScriptBuf::new_p2tr_tweaked(info.output_key());
            assert_eq!(htlc_script_pubkey(&l).unwrap(), expected.to_bytes());
        }
    }

    #[test]
    fn claim_leaf_layout() {
        let l = lock(TimelockSpec::Height(850_000));
        let (claim, refund) = htlc_leaves(&l).unwrap();
        let asm = rb_script(&claim).to_asm_string();
        assert!(asm.starts_with("OP_SIZE OP_PUSHBYTES_1 20 OP_EQUALVERIFY OP_SHA256 OP_PUSHBYTES_32"), "{asm}");
        assert!(asm.ends_with("OP_CHECKSIG"));
        let asm = rb_script(&refund).to_asm_string();
        assert!(asm.starts_with("OP_PUSHBYTES_3 50f80c OP_CLTV OP_DROP"), "{asm}");
    }

    #[test]
    fn invalid_keys_and_timelocks_are_rejected() {
        let mut l = lock(TimelockSpec::Height(850_000));
        l.keys.as_mut().unwrap().receiver = [0xff; 32];
        assert_eq!(htlc_script_pubkey(&l), Err(BtcError::InvalidKey));
        l.keys = None;
        assert_eq!(htlc_script_pubkey(&l), Err(BtcError::MissingKeys));
        let l = lock(TimelockSpec::Height(600_000_000));
        assert_eq!(htlc_script_pubkey(&l), Err(BtcError::BadTimelock));
        let l = lock(TimelockSpec::RelativeSeconds(3_600));
        assert_eq!(htlc_script_pubkey(&l), Err(BtcError::BadTimelock));
    }

    fn own_spk(seed: u8) -> Vec<u8> {
        let mut s = vec![OP_1, 32];
        s.extend(xonly(seed));
        s
    }

    fn rb_psbt(inputs: &[(u8, u64, Vec<u8>)], outputs: &[(u64, Vec<u8>)], lock_time: u32, seq: u32, sighash: Option<u32>) -> rb::Psbt {
        let tx = rb::Transaction {
            version: rb::transaction::Version::TWO,
            lock_time: rb::absolute::LockTime::from_consensus(lock_time),
            input: inputs
                .iter()
                .map(|(t, vout, _)| rb::TxIn {
                    previous_output: rb::OutPoint { txid: rb::Txid::from_byte_array([*t; 32]), vout: *vout as u32 },
                    script_sig: rb::ScriptBuf::new(),
                    sequence: rb::Sequence(seq),
                    witness: rb::Witness::new(),
                })
                .collect(),
            output: outputs
                .iter()
                .map(|(v, s)| rb::TxOut { value: rb::Amount::from_sat(*v), script_pubkey: rb_script(s) })
                .collect(),
        };
        let mut psbt = rb::Psbt::from_unsigned_tx(tx).unwrap();
        for (i, (_, value, spk)) in inputs.iter().enumerate() {
            psbt.inputs[i].witness_utxo = Some(rb::TxOut { value: rb::Amount::from_sat(100_000 + *value), script_pubkey: rb_script(spk) });
            psbt.inputs[i].sighash_type = sighash.map(rb::psbt::PsbtSighashType::from_u32);
        }
        psbt
    }

    fn rb_sighash(psbt: &rb::Psbt, index: usize, leaf: Option<&[u8]>, hash_type: rb::TapSighashType) -> Hash32 {
        let prevouts: Vec<rb::TxOut> = psbt.inputs.iter().map(|i| i.witness_utxo.clone().unwrap()).collect();
        let mut cache = rb::sighash::SighashCache::new(&psbt.unsigned_tx);
        let prevouts = rb::sighash::Prevouts::All(&prevouts);
        match leaf {
            None => cache.taproot_key_spend_signature_hash(index, &prevouts, hash_type).unwrap().to_byte_array(),
            Some(script) => {
                let lh = rb::TapLeafHash::from_script(&rb_script(script), rb::taproot::LeafVersion::TapScript);
                cache.taproot_script_spend_signature_hash(index, &prevouts, lh, hash_type).unwrap().to_byte_array()
            }
        }
    }

    #[test]
    fn lock_psbt_parsing_txid_and_sighash_match_rust_bitcoin() {
        let l = lock(TimelockSpec::Height(850_000));
        let htlc = htlc_script_pubkey(&l).unwrap();
        let own = vec![own_spk(3), own_spk(4)];
        let psbt = rb_psbt(&[(1, 0, own[0].clone()), (2, 1, own[1].clone())], &[(50_000, htlc.clone()), (1_000, own[0].clone())], 0, 0xffff_fffd, None);
        let parsed = parse_psbt(&psbt.serialize()).unwrap();
        assert_eq!(parsed.tx.txid_hex(), psbt.unsigned_tx.compute_txid().to_string());
        let binding = check_lock_psbt(&parsed, &l, 50_000, &own).unwrap();
        for i in 0..2 {
            assert_eq!(binding.sighashes[i], rb_sighash(&psbt, i, None, rb::TapSighashType::Default));
        }
        // SIGHASH_ALL also commits to everything.
        let psbt_all = rb_psbt(&[(1, 0, own[0].clone())], &[(50_000, htlc.clone())], 0, 0xffff_fffd, Some(1));
        let parsed = parse_psbt(&psbt_all.serialize()).unwrap();
        let binding = check_lock_psbt(&parsed, &l, 50_000, &own).unwrap();
        assert_eq!(binding.sighashes[0], rb_sighash(&psbt_all, 0, None, rb::TapSighashType::All));
    }

    #[test]
    fn lock_psbt_rejections() {
        let l = lock(TimelockSpec::Height(850_000));
        let htlc = htlc_script_pubkey(&l).unwrap();
        let own = vec![own_spk(3)];
        let check = |p: rb::Psbt| check_lock_psbt(&parse_psbt(&p.serialize()).unwrap(), &l, 50_000, &own);
        // Fault test 7: one extra output.
        let extra = rb_psbt(&[(1, 0, own[0].clone())], &[(50_000, htlc.clone()), (10, own_spk(9))], 0, 0, None);
        assert!(check(extra).is_err());
        // Short payment.
        assert!(check(rb_psbt(&[(1, 0, own[0].clone())], &[(49_999, htlc.clone())], 0, 0, None)).is_err());
        // Foreign input.
        assert!(check(rb_psbt(&[(1, 0, own_spk(8))], &[(50_000, htlc.clone())], 0, 0, None)).is_err());
        // No HTLC output, or two.
        assert!(check(rb_psbt(&[(1, 0, own[0].clone())], &[(50_000, own[0].clone())], 0, 0, None)).is_err());
        assert!(check(rb_psbt(&[(1, 0, own[0].clone())], &[(50_000, htlc.clone()), (50_000, htlc.clone())], 0, 0, None)).is_err());
        // Sighash types that do not commit to everything (review finding P1).
        for t in [2u32, 3, 0x81, 0x82, 0x83] {
            let p = rb_psbt(&[(1, 0, own[0].clone())], &[(50_000, htlc.clone())], 0, 0, Some(t));
            assert!(check(p).is_err(), "sighash type {t:#x}");
        }
    }

    #[test]
    fn spend_psbts() {
        let l = lock(TimelockSpec::Height(850_000));
        let htlc = htlc_script_pubkey(&l).unwrap();
        let own = vec![own_spk(5)];
        let (claim, refund) = htlc_leaves(&l).unwrap();
        let txid = [6u8; 32];
        let spend = |lock_time: u32, seq: u32, out: Vec<u8>| rb_psbt(&[(6, 0, htlc.clone())], &[(49_000, out)], lock_time, seq, None);

        let p = spend(0, 0xffff_ffff, own[0].clone());
        let parsed = parse_psbt(&p.serialize()).unwrap();
        let b = check_spend_psbt(&parsed, &l, (&txid, 0), Leaf::Claim, &own).unwrap();
        assert_eq!(b.sighashes[0], rb_sighash(&p, 0, Some(&claim), rb::TapSighashType::Default));

        let p = spend(850_000, 0xffff_fffe, own[0].clone());
        let parsed = parse_psbt(&p.serialize()).unwrap();
        let b = check_spend_psbt(&parsed, &l, (&txid, 0), Leaf::Refund, &own).unwrap();
        assert_eq!(b.sighashes[0], rb_sighash(&p, 0, Some(&refund), rb::TapSighashType::Default));

        // Refund before its timelock, refund with locktime disabled, and a foreign payee.
        for p in [spend(849_999, 0xffff_fffe, own[0].clone()), spend(850_000, 0xffff_ffff, own[0].clone()), spend(850_000, 0, own_spk(9))] {
            let parsed = parse_psbt(&p.serialize()).unwrap();
            assert!(check_spend_psbt(&parsed, &l, (&txid, 0), Leaf::Refund, &own).is_err());
        }
        // Wrong outpoint.
        let parsed = parse_psbt(&spend(0, 0, own[0].clone()).serialize()).unwrap();
        assert!(check_spend_psbt(&parsed, &l, (&[7u8; 32], 0), Leaf::Claim, &own).is_err());
    }

    #[test]
    fn malformed_psbts() {
        assert!(parse_psbt(b"psbt").is_err());
        assert!(parse_psbt(b"psbu\xff\x00").is_err());
        assert!(parse_psbt(b"psbt\xff\x00").is_err(), "no transaction");
        let l = lock(TimelockSpec::Height(850_000));
        let p = rb_psbt(&[(1, 0, own_spk(3))], &[(1, htlc_script_pubkey(&l).unwrap())], 0, 0, None);
        let mut bytes = p.serialize();
        bytes.push(0);
        assert!(parse_psbt(&bytes).is_err(), "trailing byte");
        let bytes = p.serialize();
        assert!(parse_psbt(&bytes[..bytes.len() - 3]).is_err(), "truncated");
    }
}
