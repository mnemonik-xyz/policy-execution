//! EVM profile primitives: RLP and EIP-1559 decoding, the reference HTLC ABI
//! (implementation spec 4.6), exact calldata matching (S24), contract identity
//! (S7) and the transaction binding `keccak256(0x02 ‖ rlp(fields))`.

use crate::Hash32;
use std::fmt;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EvmError {
    Rlp(&'static str),
    NotEip1559,
    Mismatch(String),
}

impl fmt::Display for EvmError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EvmError::Rlp(e) => write!(f, "RLP: {e}"),
            EvmError::NotEip1559 => f.write_str("not an unsigned EIP-1559 transaction"),
            EvmError::Mismatch(e) => write!(f, "transaction does not match the intent: {e}"),
        }
    }
}

impl std::error::Error for EvmError {}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Rlp<'a> {
    Bytes(&'a [u8]),
    List(Vec<Rlp<'a>>),
}

/// Decode one canonical RLP item and require that it fills `data`.
pub fn rlp_decode(data: &[u8]) -> Result<Rlp<'_>, EvmError> {
    let (item, used) = rlp_item(data)?;
    if used != data.len() {
        return Err(EvmError::Rlp("trailing bytes"));
    }
    Ok(item)
}

fn rlp_len(data: &[u8], n: usize) -> Result<usize, EvmError> {
    let bytes = data.get(..n).ok_or(EvmError::Rlp("truncated length"))?;
    if bytes.first() == Some(&0) {
        return Err(EvmError::Rlp("leading zero in length"));
    }
    let len = bytes.iter().try_fold(0usize, |acc, b| acc.checked_mul(256).map(|v| v + *b as usize));
    let len = len.ok_or(EvmError::Rlp("length overflow"))?;
    if len <= 55 {
        return Err(EvmError::Rlp("long form for a short payload"));
    }
    Ok(len)
}

fn rlp_item(data: &[u8]) -> Result<(Rlp<'_>, usize), EvmError> {
    let prefix = *data.first().ok_or(EvmError::Rlp("empty input"))?;
    let (is_list, offset, len) = match prefix {
        0x00..=0x7f => return Ok((Rlp::Bytes(&data[..1]), 1)),
        0x80..=0xb7 => (false, 1, (prefix - 0x80) as usize),
        0xb8..=0xbf => {
            let n = (prefix - 0xb7) as usize;
            (false, 1 + n, rlp_len(&data[1..], n)?)
        }
        0xc0..=0xf7 => (true, 1, (prefix - 0xc0) as usize),
        0xf8..=0xff => {
            let n = (prefix - 0xf7) as usize;
            (true, 1 + n, rlp_len(&data[1..], n)?)
        }
    };
    let end = offset.checked_add(len).ok_or(EvmError::Rlp("length overflow"))?;
    let payload = data.get(offset..end).ok_or(EvmError::Rlp("truncated payload"))?;
    if is_list {
        let mut items = Vec::new();
        let mut pos = 0;
        while pos < payload.len() {
            let (item, used) = rlp_item(&payload[pos..])?;
            items.push(item);
            pos += used;
        }
        Ok((Rlp::List(items), end))
    } else {
        if len == 1 && payload[0] < 0x80 {
            return Err(EvmError::Rlp("single byte below 0x80 must encode itself"));
        }
        Ok((Rlp::Bytes(payload), end))
    }
}

fn rlp_uint(item: &Rlp, max_bytes: usize) -> Result<u128, EvmError> {
    match item {
        Rlp::Bytes(b) if b.len() <= max_bytes.min(16) => {
            if b.first() == Some(&0) {
                return Err(EvmError::Rlp("leading zero in integer"));
            }
            Ok(b.iter().fold(0u128, |acc, x| acc << 8 | *x as u128))
        }
        _ => Err(EvmError::Rlp("integer expected")),
    }
}

fn rlp_bytes<'a>(item: &Rlp<'a>) -> Result<&'a [u8], EvmError> {
    match item {
        Rlp::Bytes(b) => Ok(b),
        Rlp::List(_) => Err(EvmError::Rlp("byte string expected")),
    }
}

/// An unsigned EIP-1559 transaction (EIP-2718 type 2).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Eip1559Tx {
    pub chain_id: u64,
    pub nonce: u64,
    pub max_priority_fee_per_gas: u128,
    pub max_fee_per_gas: u128,
    pub gas_limit: u64,
    pub to: Option<[u8; 20]>,
    /// Values above 2^128 − 1 are rejected; no leg amount is that large.
    pub value: u128,
    pub data: Vec<u8>,
    pub access_list_len: usize,
}

pub fn parse_unsigned(bytes: &[u8]) -> Result<Eip1559Tx, EvmError> {
    if bytes.first() != Some(&0x02) {
        return Err(EvmError::NotEip1559);
    }
    let Rlp::List(f) = rlp_decode(&bytes[1..])? else {
        return Err(EvmError::NotEip1559);
    };
    if f.len() != 9 {
        return Err(EvmError::NotEip1559);
    }
    let to = match rlp_bytes(&f[5])? {
        [] => None,
        b if b.len() == 20 => Some(b.try_into().unwrap()),
        _ => return Err(EvmError::Rlp("bad recipient")),
    };
    let Rlp::List(access) = &f[8] else {
        return Err(EvmError::Rlp("access list expected"));
    };
    Ok(Eip1559Tx {
        chain_id: rlp_uint(&f[0], 8)? as u64,
        nonce: rlp_uint(&f[1], 8)? as u64,
        max_priority_fee_per_gas: rlp_uint(&f[2], 16)?,
        max_fee_per_gas: rlp_uint(&f[3], 16)?,
        gas_limit: rlp_uint(&f[4], 8)? as u64,
        to,
        value: rlp_uint(&f[6], 16)?,
        data: rlp_bytes(&f[7])?.to_vec(),
        access_list_len: access.len(),
    })
}

/// The signing hash of an unsigned type-2 transaction.
pub fn signing_hash(unsigned: &[u8]) -> Hash32 {
    crate::keccak256(unsigned)
}

fn rlp_header(len: usize, short: u8, out: &mut Vec<u8>) {
    if len <= 55 {
        out.push(short + len as u8);
    } else {
        let be = (len as u64).to_be_bytes();
        let skip = be.iter().take_while(|b| **b == 0).count();
        out.push(short + 55 + (8 - skip) as u8);
        out.extend(&be[skip..]);
    }
}

pub fn rlp_encode_bytes(b: &[u8], out: &mut Vec<u8>) {
    if b.len() == 1 && b[0] < 0x80 {
        out.push(b[0]);
    } else {
        rlp_header(b.len(), 0x80, out);
        out.extend(b);
    }
}

pub fn rlp_encode_uint(v: u128, out: &mut Vec<u8>) {
    let be = v.to_be_bytes();
    let skip = be.iter().take_while(|b| **b == 0).count();
    rlp_encode_bytes(&be[skip..], out);
}

impl Eip1559Tx {
    /// `0x02 ‖ rlp([...])` with an empty access list; the policy signer builds
    /// transactions with this.
    pub fn encode_unsigned(&self) -> Vec<u8> {
        self.encode_with_access_list(&[0xc0])
    }

    /// `access_list` is the RLP encoding of the access list.
    pub(crate) fn encode_with_access_list(&self, access_list: &[u8]) -> Vec<u8> {
        let mut body = Vec::new();
        rlp_encode_uint(self.chain_id as u128, &mut body);
        rlp_encode_uint(self.nonce as u128, &mut body);
        rlp_encode_uint(self.max_priority_fee_per_gas, &mut body);
        rlp_encode_uint(self.max_fee_per_gas, &mut body);
        rlp_encode_uint(self.gas_limit as u128, &mut body);
        rlp_encode_bytes(self.to.as_ref().map(|a| &a[..]).unwrap_or(&[]), &mut body);
        rlp_encode_uint(self.value, &mut body);
        rlp_encode_bytes(&self.data, &mut body);
        body.extend(access_list);
        let mut out = vec![0x02];
        rlp_header(body.len(), 0xc0, &mut out);
        out.extend(body);
        out
    }
}

// ---------------------------------------------------------------------------
// Reference HTLC ABI
// ---------------------------------------------------------------------------

/// `lock(bytes32 swapId, bytes1 leg, address receiver, address refundTo, address token,
/// uint256 amount, bytes32 hashlock, uint64 timelock)`. The contract derives the key
/// `lockId = sha256(abi.encodePacked(swapId, leg, msg.sender))` (spec 3.2) and
/// reverts when that key was ever used, so no third party can take it first.
pub const LOCK_SIG: &str = "lock(bytes32,bytes1,address,address,address,uint256,bytes32,uint64)";
/// `claim(bytes32 lockId, bytes32 preimage)`.
pub const CLAIM_SIG: &str = "claim(bytes32,bytes32)";
/// `refund(bytes32 lockId)`.
pub const REFUND_SIG: &str = "refund(bytes32)";
pub const APPROVE_SIG: &str = "approve(address,uint256)";

pub fn selector(signature: &str) -> [u8; 4] {
    crate::keccak256(signature.as_bytes())[..4].try_into().unwrap()
}

fn word_address(a: &[u8; 20]) -> [u8; 32] {
    let mut w = [0u8; 32];
    w[12..].copy_from_slice(a);
    w
}

/// ABI `bytes1`: the byte first, then zero padding.
fn word_bytes1(b: u8) -> [u8; 32] {
    let mut w = [0u8; 32];
    w[0] = b;
    w
}

fn word_uint(v: u128) -> [u8; 32] {
    let mut w = [0u8; 32];
    w[16..].copy_from_slice(&v.to_be_bytes());
    w
}

fn call(signature: &str, words: &[[u8; 32]]) -> Vec<u8> {
    let mut data = selector(signature).to_vec();
    for w in words {
        data.extend(w);
    }
    data
}

/// Arguments of the reference `lock`. `token` is the zero address for the native
/// coin. For a token with a transfer fee, `amount` is the gross debit. The call
/// carries `swap_id` and the leg, not `lock_id`: the contract derives `lock_id`
/// from them and the caller.
pub struct LockCall {
    pub swap_id: Hash32,
    pub leg: crate::types::LegName,
    pub receiver: [u8; 20],
    pub refund_to: [u8; 20],
    pub token: [u8; 20],
    pub amount: u128,
    pub hashlock: Hash32,
    pub timelock: u64,
}

impl LockCall {
    pub fn calldata(&self) -> Vec<u8> {
        call(
            LOCK_SIG,
            &[
                self.swap_id,
                word_bytes1(self.leg.lock_byte()),
                word_address(&self.receiver),
                word_address(&self.refund_to),
                word_address(&self.token),
                word_uint(self.amount),
                self.hashlock,
                word_uint(self.timelock as u128),
            ],
        )
    }
}

pub fn claim_calldata(lock_id: &Hash32, preimage: &Hash32) -> Vec<u8> {
    call(CLAIM_SIG, &[*lock_id, *preimage])
}

pub fn refund_calldata(lock_id: &Hash32) -> Vec<u8> {
    call(REFUND_SIG, &[*lock_id])
}

pub fn approve_calldata(spender: &[u8; 20], amount: u128) -> Vec<u8> {
    call(APPROVE_SIG, &[word_address(spender), word_uint(amount)])
}

/// What the decoded transaction must be, byte for byte.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EvmIntent {
    pub chain_id: u64,
    pub to: [u8; 20],
    pub value: u128,
    pub data: Vec<u8>,
}

/// S24 for EVM: the chain id, recipient, value and calldata equal the intent, the
/// access list is empty and contract creation is impossible. Fees and nonce are
/// the signer's own choice and are bound through the signing hash.
pub fn check_tx(unsigned: &[u8], intent: &EvmIntent) -> Result<Hash32, EvmError> {
    let tx = parse_unsigned(unsigned)?;
    let mismatch = |what: &str| Err(EvmError::Mismatch(what.into()));
    if tx.chain_id != intent.chain_id {
        return mismatch("chain id");
    }
    if tx.to != Some(intent.to) {
        return mismatch("recipient");
    }
    if tx.value != intent.value {
        return mismatch("value");
    }
    if tx.data != intent.data {
        return mismatch("calldata");
    }
    if tx.access_list_len != 0 {
        return mismatch("access list");
    }
    Ok(signing_hash(unsigned))
}

/// Storage slots that can hold a proxy's target or admin (spec 8.4): 32-byte keys
/// in hex. The observation adapter reads the word at each slot of the contract at
/// the fact block (`eth_getStorageAt`, or `eth_getProof`, EIP-1186) and reports it
/// in the `ProxySlots` field of the same name.
pub mod proxy_slot {
    /// EIP-1967 implementation: `keccak256("eip1967.proxy.implementation") - 1`.
    pub const EIP1967_IMPLEMENTATION: &str = "360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc";
    /// EIP-1967 admin: `keccak256("eip1967.proxy.admin") - 1`.
    pub const EIP1967_ADMIN: &str = "b53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103";
    /// EIP-1967 beacon: `keccak256("eip1967.proxy.beacon") - 1`.
    pub const EIP1967_BEACON: &str = "a3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50";
    /// ZeppelinOS, before EIP-1967: `keccak256("org.zeppelinos.proxy.implementation")`.
    pub const ZOS_IMPLEMENTATION: &str = "7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3";
    /// ZeppelinOS, before EIP-1967: `keccak256("org.zeppelinos.proxy.admin")`.
    pub const ZOS_ADMIN: &str = "10d6a54a4754c8869d6886b5f5d7fbfa5b4522237ea5c60d11bc4e7a1ff9390b";
    /// EIP-1822 (UUPS before EIP-1967): `keccak256("PROXIABLE")`.
    pub const EIP1822_PROXIABLE: &str = "c5f16f0fcc639fa48a6947836d9850f504798523bf8c9a3a87d5876cf622bcf7";
}

/// The EIP-2535 loupe function. Every diamond implements it; S7 needs the call to
/// revert. The adapter calls it with the 4-byte selector and no arguments.
pub const FACET_ADDRESSES_SIG: &str = "facetAddresses()";

/// The words at the `proxy_slot` slots of a contract. A zero word is unset.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProxySlots {
    pub eip1967_implementation: Hash32,
    pub eip1967_admin: Hash32,
    pub eip1967_beacon: Hash32,
    pub zos_implementation: Hash32,
    pub zos_admin: Hash32,
    pub eip1822_proxiable: Hash32,
}

/// The result of the call `facetAddresses()` (EIP-2535) to the contract at the fact
/// block (`eth_call`). `Reverted` means that the call ran at that block and ended in
/// REVERT or another exceptional halt. A JSON-RPC or transport error, a timeout or a
/// node that cannot run the call is no result: the adapter then reports no contract
/// observation, and S7 fails. It never reports such an error as `Reverted`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Loupe {
    /// The call reverted: the contract has no diamond loupe.
    Reverted,
    /// The call returned, with data or without: a diamond, or a contract that S7
    /// cannot tell from one.
    Returned,
}

/// Observed facts about a contract (spec 8.4), read at one finalized block.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ContractFacts {
    /// `EXTCODEHASH` (EIP-1052) of the contract.
    pub code_hash: Hash32,
    pub slots: ProxySlots,
    pub loupe: Loupe,
    /// The address in the EIP-1967 implementation slot and its `EXTCODEHASH`, read
    /// at the same block. `None` when that slot is zero.
    pub implementation_code: Option<([u8; 20], Hash32)>,
}

/// The proxy pattern that `ContractFacts` show.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Proxy {
    /// No proxy slot is set and the loupe call reverts.
    None,
    /// EIP-1967 with an address in the implementation slot and in the admin slot,
    /// and no other proxy slot set.
    Eip1967 { implementation: [u8; 20], admin: [u8; 20] },
    /// A beacon, a legacy slot, a diamond loupe, an implementation without an admin
    /// (UUPS) or an admin without an implementation, or a slot word that is not an
    /// address. S7 rejects it for every pin.
    Unsupported,
}

/// The address in a slot word: 12 zero bytes, then 20 bytes that are not all zero.
fn address_word(word: &Hash32) -> Option<[u8; 20]> {
    let (high, low) = word.split_at(12);
    if high.iter().any(|b| *b != 0) || low.iter().all(|b| *b == 0) {
        return None;
    }
    let mut address = [0u8; 20];
    address.copy_from_slice(low);
    Some(address)
}

impl ContractFacts {
    pub fn proxy(&self) -> Proxy {
        let s = &self.slots;
        let others = [s.eip1967_beacon, s.zos_implementation, s.zos_admin, s.eip1822_proxiable];
        if self.loupe != Loupe::Reverted || others.iter().any(|w| *w != [0; 32]) {
            return Proxy::Unsupported;
        }
        if s.eip1967_implementation == [0; 32] && s.eip1967_admin == [0; 32] {
            return Proxy::None;
        }
        match (address_word(&s.eip1967_implementation), address_word(&s.eip1967_admin)) {
            (Some(implementation), Some(admin)) => Proxy::Eip1967 { implementation, admin },
            _ => Proxy::Unsupported,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContractPin {
    /// Hex address.
    pub address: String,
    #[serde(with = "crate::enc::hex32")]
    pub code_hash: Hash32,
    /// An EIP-1967 proxy is accepted only with its admin, implementation and
    /// implementation code hash pinned. No pin accepts another proxy pattern.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub proxy: Option<ProxyPin>,
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProxyPin {
    /// Hex address.
    pub admin: String,
    /// Hex address.
    pub implementation: String,
    /// `EXTCODEHASH` of the implementation: the reviewed logic code.
    #[serde(with = "crate::enc::hex32")]
    pub implementation_code_hash: Hash32,
}

impl ProxyPin {
    pub fn admin_bytes(&self) -> Option<[u8; 20]> {
        crate::from_hex_array(&self.admin)
    }

    pub fn implementation_bytes(&self) -> Option<[u8; 20]> {
        crate::from_hex_array(&self.implementation)
    }
}

impl ContractPin {
    pub fn address_bytes(&self) -> Option<[u8; 20]> {
        crate::from_hex_array(&self.address)
    }

    /// The pin is well formed: every address parses (`validate_policy`).
    pub fn well_formed(&self) -> bool {
        self.address_bytes().is_some() && self.proxy.as_ref().is_none_or(|p| p.admin_bytes().is_some() && p.implementation_bytes().is_some())
    }

    /// S7 for EVM: address and code hash match. A contract without proxy facts
    /// matches a pin without `proxy`. An EIP-1967 proxy matches its pinned admin,
    /// implementation and implementation code hash. Every other proxy pattern fails.
    pub fn matches(&self, address: &[u8; 20], facts: &ContractFacts) -> bool {
        if self.address_bytes().as_ref() != Some(address) || self.code_hash != facts.code_hash {
            return false;
        }
        match (&self.proxy, facts.proxy()) {
            (None, Proxy::None) => facts.implementation_code.is_none(),
            (Some(p), Proxy::Eip1967 { implementation, admin }) => {
                p.implementation_bytes() == Some(implementation)
                    && p.admin_bytes() == Some(admin)
                    && facts.implementation_code == Some((implementation, p.implementation_code_hash))
            }
            _ => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_selectors() {
        assert_eq!(crate::to_hex(&selector("transfer(address,uint256)")), "a9059cbb");
        assert_eq!(crate::to_hex(&selector(APPROVE_SIG)), "095ea7b3");
    }

    #[test]
    fn rlp_canonical_rules() {
        assert_eq!(rlp_decode(&[0x82, 0x04, 0x00]).unwrap(), Rlp::Bytes(&[4, 0]));
        assert!(rlp_decode(&[0x81, 0x05]).is_err(), "non-canonical single byte");
        assert!(rlp_decode(&[0xb8, 0x01, 0x05]).is_err(), "long form for short");
        assert!(rlp_decode(&[0x82, 0x04]).is_err(), "truncated");
        assert!(rlp_decode(&[0x01, 0x02]).is_err(), "trailing");
        assert_eq!(rlp_decode(&[0xc2, 0x01, 0x80]).unwrap(), Rlp::List(vec![Rlp::Bytes(&[1]), Rlp::Bytes(&[])]));
        assert!(rlp_uint(&Rlp::Bytes(&[0, 1]), 8).is_err(), "leading zero");
    }

    /// Hand-encoded per EIP-1559 and the RLP rules: chain 1, nonce 0, fees 1 and 2,
    /// gas 21000, 1 wei, no data, empty access list. The body is 31 bytes (0xdf).
    #[test]
    fn eip1559_known_encoding() {
        let tx = Eip1559Tx {
            chain_id: 1,
            nonce: 0,
            max_priority_fee_per_gas: 1,
            max_fee_per_gas: 2,
            gas_limit: 21_000,
            to: Some(crate::from_hex_array("70997970C51812dc3A010C7d01b50e0d17dc79C8").unwrap()),
            value: 1,
            data: vec![],
            access_list_len: 0,
        };
        let bytes = tx.encode_unsigned();
        assert_eq!(
            crate::to_hex(&bytes),
            "02df0180010282520894" .to_owned() + "70997970c51812dc3a010c7d01b50e0d17dc79c8" + "0180c0"
        );
        assert_eq!(parse_unsigned(&bytes).unwrap(), tx);
    }

    #[test]
    fn eip1559_round_trip() {
        let tx = Eip1559Tx {
            chain_id: 1,
            nonce: 7,
            max_priority_fee_per_gas: 2_000_000_000,
            max_fee_per_gas: 100_000_000_000,
            gas_limit: 120_000,
            to: Some([0x11; 20]),
            value: 10u128.pow(18),
            data: vec![0xde, 0xad, 0xbe, 0xef],
            access_list_len: 0,
        };
        let bytes = tx.encode_unsigned();
        assert_eq!(parse_unsigned(&bytes).unwrap(), tx);
        assert_eq!(signing_hash(&bytes), crate::keccak256(&bytes));
        let long = Eip1559Tx { data: vec![1; 300], ..tx };
        assert_eq!(parse_unsigned(&long.encode_unsigned()).unwrap().data.len(), 300);
    }

    fn intent() -> EvmIntent {
        let call = LockCall {
            swap_id: [1; 32],
            leg: crate::types::LegName::B,
            receiver: [2; 20],
            refund_to: [3; 20],
            token: [0; 20],
            amount: 10u128.pow(18),
            hashlock: [4; 32],
            timelock: 1_800_000_000,
        };
        EvmIntent { chain_id: 1, to: [0x11; 20], value: 10u128.pow(18), data: call.calldata() }
    }

    fn tx_for(i: &EvmIntent) -> Eip1559Tx {
        Eip1559Tx {
            chain_id: i.chain_id,
            nonce: 0,
            max_priority_fee_per_gas: 1,
            max_fee_per_gas: 2,
            gas_limit: 200_000,
            to: Some(i.to),
            value: i.value,
            data: i.data.clone(),
            access_list_len: 0,
        }
    }

    #[test]
    fn intent_matching() {
        let i = intent();
        assert_eq!(i.data.len(), 4 + 8 * 32);
        // The leg is an ABI bytes1: 0x42, then zero padding.
        assert_eq!(&i.data[4 + 32..4 + 64], &{ let mut w = [0u8; 32]; w[0] = 0x42; w });
        assert!(check_tx(&tx_for(&i).encode_unsigned(), &i).is_ok());
        let cases: Vec<Box<dyn Fn(&mut Eip1559Tx)>> = vec![
            Box::new(|t| t.chain_id = 10),
            Box::new(|t| t.to = Some([0x12; 20])),
            Box::new(|t| t.to = None),
            Box::new(|t| t.value += 1),
            Box::new(|t| t.data[40] ^= 1),
            Box::new(|t| t.data.push(0)),
        ];
        for (n, mutate) in cases.iter().enumerate() {
            let mut t = tx_for(&i);
            mutate(&mut t);
            assert!(check_tx(&t.encode_unsigned(), &i).is_err(), "case {n}");
        }
        assert!(check_tx(&[0x01, 0xc0], &i).is_err(), "legacy type");
        // A non-empty access list.
        let bytes = tx_for(&i).encode_with_access_list(&[0xc1, 0x80]);
        assert_eq!(parse_unsigned(&bytes).unwrap().access_list_len, 1);
        assert!(check_tx(&bytes, &i).is_err());
    }

    /// Spec 3.2: the reference contract's `sha256(abi.encodePacked(swapId, leg,
    /// msg.sender))` is `lock_id` with the 20-byte caller as the sender bytes.
    #[test]
    fn contract_lock_id_is_the_spec_derivation() {
        use crate::types::{lock_id, LegName};
        let (swap_id, sender) = ([0x51u8; 32], [0xbbu8; 20]);
        let mut packed = swap_id.to_vec();
        packed.push(0x42);
        packed.extend(sender);
        assert_eq!(packed.len(), 32 + 1 + 20);
        assert_eq!(crate::sha256(&packed), lock_id(&swap_id, LegName::B, &sender));
        assert_ne!(lock_id(&swap_id, LegName::A, &sender), lock_id(&swap_id, LegName::B, &sender));
        // claim and refund name the lock by lock_id.
        let id = lock_id(&swap_id, LegName::B, &sender);
        assert_eq!(&claim_calldata(&id, &[9; 32])[4..36], &id);
        assert_eq!(&refund_calldata(&id)[4..], &id);
    }

    #[test]
    fn approve_is_exact() {
        let data = approve_calldata(&[0x11; 20], 1_000_000);
        assert_eq!(&data[..4], &selector(APPROVE_SIG));
        assert_eq!(&data[4 + 12..4 + 32], &[0x11; 20]);
        assert_eq!(u128::from_be_bytes(data[4 + 48..].try_into().unwrap()), 1_000_000);
    }

    /// The slot keys follow from their published labels, and the loupe selector is
    /// the EIP-2535 `facetAddresses()` selector.
    #[test]
    fn proxy_slot_constants() {
        let minus_one = |label: &str| {
            let mut h = crate::keccak256(label.as_bytes());
            for b in h.iter_mut().rev() {
                let (v, borrow) = b.overflowing_sub(1);
                *b = v;
                if !borrow {
                    break;
                }
            }
            crate::to_hex(&h)
        };
        let plain = |label: &str| crate::to_hex(&crate::keccak256(label.as_bytes()));
        assert_eq!(proxy_slot::EIP1967_IMPLEMENTATION, minus_one("eip1967.proxy.implementation"));
        assert_eq!(proxy_slot::EIP1967_ADMIN, minus_one("eip1967.proxy.admin"));
        assert_eq!(proxy_slot::EIP1967_BEACON, minus_one("eip1967.proxy.beacon"));
        assert_eq!(proxy_slot::ZOS_IMPLEMENTATION, plain("org.zeppelinos.proxy.implementation"));
        assert_eq!(proxy_slot::ZOS_ADMIN, plain("org.zeppelinos.proxy.admin"));
        assert_eq!(proxy_slot::EIP1822_PROXIABLE, plain("PROXIABLE"));
        assert_eq!(crate::to_hex(&selector(FACET_ADDRESSES_SIG)), "52ef6b2c");
    }

    fn word(address: [u8; 20]) -> Hash32 {
        let mut w = [0u8; 32];
        w[12..].copy_from_slice(&address);
        w
    }

    fn unset() -> ProxySlots {
        ProxySlots { eip1967_implementation: [0; 32], eip1967_admin: [0; 32], eip1967_beacon: [0; 32], zos_implementation: [0; 32], zos_admin: [0; 32], eip1822_proxiable: [0; 32] }
    }

    fn plain() -> ContractFacts {
        ContractFacts { code_hash: [5; 32], slots: unset(), loupe: Loupe::Reverted, implementation_code: None }
    }

    /// An EIP-1967 proxy with implementation 0x07.., admin 0x08.. and implementation code hash 0x09...
    fn proxied() -> ContractFacts {
        let slots = ProxySlots { eip1967_implementation: word([7; 20]), eip1967_admin: word([8; 20]), ..unset() };
        ContractFacts { slots, implementation_code: Some(([7; 20], [9; 32])), ..plain() }
    }

    fn pin() -> ContractPin {
        ContractPin { address: format!("0x{}", crate::to_hex(&[0x11; 20])), code_hash: [5; 32], proxy: None }
    }

    fn proxy_pin() -> ContractPin {
        let proxy = ProxyPin { admin: crate::to_hex(&[8; 20]), implementation: crate::to_hex(&[7; 20]), implementation_code_hash: [9; 32] };
        ContractPin { proxy: Some(proxy), ..pin() }
    }

    #[test]
    fn contract_pins() {
        assert!(pin().matches(&[0x11; 20], &plain()));
        assert!(!pin().matches(&[0x12; 20], &plain()), "look-alike address");
        assert!(!pin().matches(&[0x11; 20], &ContractFacts { code_hash: [6; 32], ..plain() }), "other code");
        assert!(!pin().matches(&[0x11; 20], &ContractFacts { implementation_code: Some(([7; 20], [9; 32])), ..plain() }), "implementation code without a proxy slot");
        // Fault test 6: a proxy HTLC is rejected unless its admin, implementation and implementation code are pinned.
        assert!(!pin().matches(&[0x11; 20], &proxied()));
        assert!(proxy_pin().matches(&[0x11; 20], &proxied()));
        assert!(!proxy_pin().matches(&[0x11; 20], &plain()), "a pinned proxy that is no proxy now");
        let mut f = proxied();
        f.slots.eip1967_admin = word([10; 20]);
        assert!(!proxy_pin().matches(&[0x11; 20], &f), "other admin");
        let mut f = proxied();
        f.slots.eip1967_implementation = word([10; 20]);
        f.implementation_code = Some(([10; 20], [9; 32]));
        assert!(!proxy_pin().matches(&[0x11; 20], &f), "other implementation with the same code");
        assert!(!proxy_pin().matches(&[0x11; 20], &ContractFacts { implementation_code: Some(([7; 20], [6; 32])), ..proxied() }), "other implementation code");
        assert!(!proxy_pin().matches(&[0x11; 20], &ContractFacts { implementation_code: Some(([10; 20], [9; 32])), ..proxied() }), "code hash read at another address");
        assert!(!proxy_pin().matches(&[0x11; 20], &ContractFacts { implementation_code: None, ..proxied() }), "implementation code not read");
    }

    /// Fault test 6: no pin accepts a beacon, a legacy slot, a diamond, a UUPS proxy
    /// without an admin slot, or a slot word that is not an address.
    #[test]
    fn other_proxy_patterns_fail() {
        type Mutation = fn(&mut ContractFacts);
        let cases: [(&str, Mutation); 9] = [
            ("beacon", |f| f.slots.eip1967_beacon = word([12; 20])),
            ("zeppelinos implementation", |f| f.slots.zos_implementation = word([7; 20])),
            ("zeppelinos admin", |f| f.slots.zos_admin = word([8; 20])),
            ("eip-1822 proxiable", |f| f.slots.eip1822_proxiable = word([7; 20])),
            ("diamond loupe", |f| f.loupe = Loupe::Returned),
            ("no admin (uups)", |f| f.slots.eip1967_admin = [0; 32]),
            ("no implementation", |f| f.slots.eip1967_implementation = [0; 32]),
            ("implementation word with high bytes", |f| f.slots.eip1967_implementation[0] = 1),
            ("admin word with high bytes", |f| f.slots.eip1967_admin[11] = 1),
        ];
        for (name, mutate) in &cases {
            let mut f = proxied();
            mutate(&mut f);
            assert_eq!(f.proxy(), Proxy::Unsupported, "{name}");
            assert!(!proxy_pin().matches(&[0x11; 20], &f), "pinned: {name}");
            assert!(!pin().matches(&[0x11; 20], &f), "unpinned: {name}");
            // The pattern alone on a contract that is otherwise plain.
            let mut f = plain();
            mutate(&mut f);
            assert!(f == plain() || !pin().matches(&[0x11; 20], &f), "plain: {name}");
        }
        assert_eq!(plain().proxy(), Proxy::None);
        assert_eq!(proxied().proxy(), Proxy::Eip1967 { implementation: [7; 20], admin: [8; 20] });
    }

    #[test]
    fn malformed_proxy_pins() {
        assert!(pin().well_formed() && proxy_pin().well_formed());
        let mut p = proxy_pin();
        if let Some(x) = p.proxy.as_mut() {
            x.admin = "0x1234".into();
        }
        assert!(!p.well_formed(), "short admin");
        let mut p = proxy_pin();
        if let Some(x) = p.proxy.as_mut() {
            x.implementation = "zz".into();
        }
        assert!(!p.well_formed(), "implementation not hex");
        assert!(!ContractPin { address: "0x11".into(), ..pin() }.well_formed());
        // The implementation code hash is required in the policy text.
        let text = r#"{"address":"0x1111111111111111111111111111111111111111","code_hash":"0505050505050505050505050505050505050505050505050505050505050505","proxy":{"admin":"0808080808080808080808080808080808080808","implementation":"0707070707070707070707070707070707070707"}}"#;
        assert!(serde_json::from_str::<ContractPin>(text).is_err());
    }
}
