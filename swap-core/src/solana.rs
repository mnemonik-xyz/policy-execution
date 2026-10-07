//! Solana profile primitives: legacy and version 0 message decoding, the allowed
//! instruction set by mode (spec 4.2 as fixed), the reference HTLC instruction
//! layout (implementation spec 4.6), program and escrow identity (S7) and the
//! transaction binding `blake3(message bytes)`.

use crate::Hash32;
use std::collections::BTreeMap;
use std::fmt;

pub const SYSTEM_PROGRAM: &str = "11111111111111111111111111111111";
pub const COMPUTE_BUDGET_PROGRAM: &str = "ComputeBudget111111111111111111111111111111";
pub const TOKEN_PROGRAM: &str = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
pub const TOKEN_2022_PROGRAM: &str = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";
pub const ATA_PROGRAM: &str = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";
pub const ED25519_PROGRAM: &str = "Ed25519SigVerify111111111111111111111111111";

/// PDA seed prefix of the reference HTLC escrow: `[b"htlc", swap_id]`.
pub const ESCROW_SEED: &[u8] = b"htlc";

pub fn key(base58: &str) -> Hash32 {
    bs58::decode(base58).into_vec().ok().and_then(|v| v.try_into().ok()).expect("valid program id constant")
}

pub fn parse_key(base58: &str) -> Option<Hash32> {
    bs58::decode(base58).into_vec().ok()?.try_into().ok()
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SolError {
    Decode(&'static str),
    LookupsUnresolved,
    Mismatch(String),
}

impl fmt::Display for SolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SolError::Decode(e) => write!(f, "message: {e}"),
            SolError::LookupsUnresolved => f.write_str("an address lookup table or index is not in the observed tables"),
            SolError::Mismatch(e) => write!(f, "transaction does not match the intent: {e}"),
        }
    }
}

impl std::error::Error for SolError {}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Instruction {
    pub program_index: u8,
    pub accounts: Vec<u8>,
    pub data: Vec<u8>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LookupTable {
    pub key: Hash32,
    pub writable: Vec<u8>,
    pub readonly: Vec<u8>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Message {
    pub version0: bool,
    pub header: [u8; 3],
    pub account_keys: Vec<Hash32>,
    pub recent_blockhash: Hash32,
    pub instructions: Vec<Instruction>,
    pub lookups: Vec<LookupTable>,
}

struct Reader<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], SolError> {
        let end = self.pos.checked_add(n).ok_or(SolError::Decode("length overflow"))?;
        let s = self.data.get(self.pos..end).ok_or(SolError::Decode("truncated"))?;
        self.pos = end;
        Ok(s)
    }
    fn u8(&mut self) -> Result<u8, SolError> {
        Ok(self.take(1)?[0])
    }
    /// Canonical compact-u16 ("shortvec").
    fn compact(&mut self) -> Result<usize, SolError> {
        let mut value = 0usize;
        for i in 0..3 {
            let b = self.u8()?;
            value |= ((b & 0x7f) as usize) << (7 * i);
            if b & 0x80 == 0 {
                if i > 0 && b == 0 {
                    return Err(SolError::Decode("non-canonical length"));
                }
                if value > u16::MAX as usize {
                    return Err(SolError::Decode("length overflow"));
                }
                return Ok(value);
            }
        }
        Err(SolError::Decode("length too long"))
    }
    fn bytes(&mut self) -> Result<Vec<u8>, SolError> {
        let n = self.compact()?;
        Ok(self.take(n)?.to_vec())
    }
    fn key(&mut self) -> Result<Hash32, SolError> {
        Ok(self.take(32)?.try_into().unwrap())
    }
}

pub fn parse_message(bytes: &[u8]) -> Result<Message, SolError> {
    let mut r = Reader { data: bytes, pos: 0 };
    let first = *bytes.first().ok_or(SolError::Decode("empty"))?;
    let version0 = first & 0x80 != 0;
    if version0 {
        if first != 0x80 {
            return Err(SolError::Decode("unsupported message version"));
        }
        r.u8()?;
    }
    let header = [r.u8()?, r.u8()?, r.u8()?];
    let n_keys = r.compact()?;
    let account_keys = (0..n_keys).map(|_| r.key()).collect::<Result<Vec<_>, _>>()?;
    // The fee payer is a writable signer; read-only counts stay inside their groups.
    if account_keys.is_empty()
        || header[0] == 0
        || header[0] as usize > account_keys.len()
        || header[1] >= header[0]
        || header[2] as usize > account_keys.len() - header[0] as usize
    {
        return Err(SolError::Decode("bad header"));
    }
    let recent_blockhash = r.key()?;
    let n_ix = r.compact()?;
    let mut instructions = Vec::new();
    for _ in 0..n_ix {
        let program_index = r.u8()?;
        let accounts = r.bytes()?;
        let data = r.bytes()?;
        instructions.push(Instruction { program_index, accounts, data });
    }
    let mut lookups = Vec::new();
    if version0 {
        let n = r.compact()?;
        for _ in 0..n {
            lookups.push(LookupTable { key: r.key()?, writable: r.bytes()?, readonly: r.bytes()? });
        }
    }
    if r.pos != bytes.len() {
        return Err(SolError::Decode("trailing bytes"));
    }
    Ok(Message { version0, header, account_keys, recent_blockhash, instructions, lookups })
}

/// Address lookup tables as the signer read them from the chain: the table
/// address and its addresses in order. Never taken from the proposal.
pub type LookupTables = BTreeMap<Hash32, Vec<Hash32>>;

impl Message {
    /// Static keys, then every writable lookup address, then every read-only one.
    /// The addresses come from `tables` by the table address and index in the
    /// message. Entries of a table never change once written.
    pub fn all_keys(&self, tables: &LookupTables) -> Result<Vec<Hash32>, SolError> {
        let mut keys = self.account_keys.clone();
        for writable in [true, false] {
            for l in &self.lookups {
                let table = tables.get(&l.key).ok_or(SolError::LookupsUnresolved)?;
                let indexes = if writable { &l.writable } else { &l.readonly };
                for i in indexes {
                    keys.push(*table.get(*i as usize).ok_or(SolError::LookupsUnresolved)?);
                }
            }
        }
        Ok(keys)
    }

    /// Privileges of the account at `index` of `all_keys`.
    fn meta(&self, keys: &[Hash32], index: u8) -> Option<AccountMeta> {
        let i = index as usize;
        let key = *keys.get(i)?;
        let (signers, ro_signed, ro_unsigned) = (self.header[0] as usize, self.header[1] as usize, self.header[2] as usize);
        let n_static = self.account_keys.len();
        let writable_lookups: usize = self.lookups.iter().map(|l| l.writable.len()).sum();
        let writable = if i < signers {
            i < signers - ro_signed
        } else if i < n_static {
            i < n_static - ro_unsigned
        } else {
            i - n_static < writable_lookups
        };
        Some(AccountMeta { key, signer: i < signers, writable })
    }
}

/// An instruction account with its privileges in the message.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AccountMeta {
    pub key: Hash32,
    pub signer: bool,
    pub writable: bool,
}

impl AccountMeta {
    pub fn signer_writable(key: Hash32) -> Self {
        AccountMeta { key, signer: true, writable: true }
    }
    pub fn writable(key: Hash32) -> Self {
        AccountMeta { key, signer: false, writable: true }
    }
    pub fn readonly(key: Hash32) -> Self {
        AccountMeta { key, signer: false, writable: false }
    }
}

/// Native programs that a mode needs (spec 4.2, review finding on S24).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SolanaMode {
    /// Durable-nonce refund: the System Program `AdvanceNonceAccount` instruction,
    /// first, with these accounts.
    pub durable_nonce: Option<NonceAccounts>,
    /// Tier E2: one Ed25519 program instruction over exactly this key and message.
    pub ed25519: Option<Ed25519Expect>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NonceAccounts {
    pub nonce: Hash32,
    pub authority: Hash32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Ed25519Expect {
    pub pubkey: Hash32,
    pub message: Vec<u8>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SolanaIntent {
    pub htlc_program: Hash32,
    pub htlc_data: Vec<u8>,
    /// The exact accounts of the HTLC instruction, in order, with their privileges.
    pub htlc_accounts: Vec<AccountMeta>,
    pub fee_payer: Hash32,
    pub mode: SolanaMode,
}

fn ed25519_ok(data: &[u8], expect: &Ed25519Expect) -> bool {
    if data.len() < 16 || data[0] != 1 {
        return false;
    }
    let u16_at = |i: usize| u16::from_le_bytes([data[i], data[i + 1]]) as usize;
    let (pk_off, msg_off, msg_size) = (u16_at(6), u16_at(10), u16_at(12));
    let same_ix = [4, 8, 14].iter().all(|i| u16_at(*i) == u16::MAX as usize);
    same_ix
        && data.get(pk_off..pk_off + 32) == Some(&expect.pubkey[..])
        && data.get(msg_off..msg_off + msg_size) == Some(&expect.message[..])
}

/// S24 for Solana: every instruction is the HTLC call of the intent or an allowed
/// helper for the mode; the fee payer is own. Returns the binding hash.
pub fn check_message(bytes: &[u8], tables: &LookupTables, intent: &SolanaIntent) -> Result<Hash32, SolError> {
    let msg = parse_message(bytes)?;
    let keys = msg.all_keys(tables)?;
    let mismatch = |what: &str| Err(SolError::Mismatch(what.into()));
    if keys[0] != intent.fee_payer {
        return mismatch("fee payer");
    }
    let account = |ix: &Instruction, n: usize| ix.accounts.get(n).and_then(|i| keys.get(*i as usize)).copied();
    let mut htlc_calls = 0;
    let mut ed25519_calls = 0;
    for (n, ix) in msg.instructions.iter().enumerate() {
        let program = *keys.get(ix.program_index as usize).ok_or(SolError::Decode("program index"))?;
        if program == intent.htlc_program {
            if ix.data != intent.htlc_data {
                return mismatch("HTLC instruction data");
            }
            let metas: Option<Vec<AccountMeta>> = ix.accounts.iter().map(|i| msg.meta(&keys, *i)).collect();
            if metas.as_ref() != Some(&intent.htlc_accounts) {
                return mismatch("HTLC instruction accounts or privileges");
            }
            htlc_calls += 1;
        } else if program == key(COMPUTE_BUDGET_PROGRAM) {
            let ok = matches!((ix.data.first(), ix.data.len()), (Some(2), 5) | (Some(3), 9));
            if !ok {
                return mismatch("compute budget instruction other than unit limit or unit price");
            }
        } else if program == key(SYSTEM_PROGRAM) {
            let Some(nonce) = &intent.mode.durable_nonce else {
                return mismatch("System Program outside durable-nonce mode");
            };
            if n != 0 || ix.data != [4, 0, 0, 0] || account(ix, 0) != Some(nonce.nonce) || account(ix, 2) != Some(nonce.authority) {
                return mismatch("System Program instruction other than the pinned nonce advance");
            }
        } else if program == key(ED25519_PROGRAM) {
            let Some(expect) = &intent.mode.ed25519 else {
                return mismatch("Ed25519 program outside tier E2");
            };
            if !ed25519_ok(&ix.data, expect) {
                return mismatch("Ed25519 instruction over another key or message");
            }
            ed25519_calls += 1;
        } else if program == key(TOKEN_PROGRAM) || program == key(TOKEN_2022_PROGRAM) {
            if ix.data != [17] {
                return mismatch("token instruction other than SyncNative");
            }
        } else if program == key(ATA_PROGRAM) {
            if !(ix.data.is_empty() || ix.data == [0] || ix.data == [1]) {
                return mismatch("associated token account instruction other than create");
            }
        } else {
            return mismatch("instruction of a program outside the allowed set");
        }
    }
    if htlc_calls != 1 {
        return mismatch("expected exactly one HTLC instruction");
    }
    if intent.mode.durable_nonce.is_some() {
        let first = msg.instructions.first().and_then(|ix| keys.get(ix.program_index as usize));
        if first != Some(&key(SYSTEM_PROGRAM)) {
            return mismatch("durable-nonce mode needs the nonce advance first");
        }
    }
    if intent.mode.ed25519.is_some() && ed25519_calls != 1 {
        return mismatch("tier E2 needs exactly one Ed25519 instruction");
    }
    Ok(crate::blake3(bytes))
}

// ---------------------------------------------------------------------------
// Reference HTLC instruction data
// ---------------------------------------------------------------------------

/// `mint` is all zero for native SOL. For a Token-2022 mint with a transfer fee,
/// `amount` is the gross debit.
pub struct LockData {
    pub swap_id: Hash32,
    pub receiver: Hash32,
    pub refund_to: Hash32,
    pub mint: Hash32,
    pub amount: u64,
    pub hashlock: Hash32,
    pub timelock: i64,
}

impl LockData {
    pub fn encode(&self) -> Vec<u8> {
        let mut d = vec![0u8];
        d.extend(self.swap_id);
        d.extend(self.receiver);
        d.extend(self.refund_to);
        d.extend(self.mint);
        d.extend(self.amount.to_le_bytes());
        d.extend(self.hashlock);
        d.extend(self.timelock.to_le_bytes());
        d
    }
}

pub fn claim_data(swap_id: &Hash32, preimage: &Hash32) -> Vec<u8> {
    let mut d = vec![1u8];
    d.extend(swap_id);
    d.extend(preimage);
    d
}

pub fn refund_data(swap_id: &Hash32) -> Vec<u8> {
    let mut d = vec![2u8];
    d.extend(swap_id);
    d
}

/// A token leg's mint and the program that owns the mint (Token or Token-2022).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TokenAccounts {
    pub mint: Hash32,
    pub token_program: Hash32,
}

/// The associated token account of `owner` for `mint`.
pub fn associated_token_address(owner: &Hash32, token: &TokenAccounts) -> Option<Hash32> {
    find_program_address(&[owner, &token.token_program, &token.mint], &key(ATA_PROGRAM)).map(|(a, _)| a)
}

/// A key that occurs twice has the same privileges at both places in a message.
fn merge_duplicates(mut metas: Vec<AccountMeta>) -> Vec<AccountMeta> {
    let all = metas.clone();
    for m in &mut metas {
        m.signer = all.iter().any(|o| o.key == m.key && o.signer);
        m.writable = all.iter().any(|o| o.key == m.key && o.writable);
    }
    metas
}

/// Reference HTLC accounts of a lock (implementation spec 4.6): the sender (signer,
/// writable), the escrow PDA (writable); for a token, the sender's and the escrow's
/// associated token accounts (writable), the mint and the token program; then the
/// System Program.
pub fn lock_accounts(program: &Hash32, swap_id: &Hash32, sender: &Hash32, token: Option<&TokenAccounts>) -> Option<Vec<AccountMeta>> {
    let escrow = escrow_address(program, swap_id)?;
    let mut v = vec![AccountMeta::signer_writable(*sender), AccountMeta::writable(escrow)];
    if let Some(t) = token {
        v.push(AccountMeta::writable(associated_token_address(sender, t)?));
        v.push(AccountMeta::writable(associated_token_address(&escrow, t)?));
        v.push(AccountMeta::readonly(t.mint));
        v.push(AccountMeta::readonly(t.token_program));
    }
    v.push(AccountMeta::readonly(key(SYSTEM_PROGRAM)));
    Some(merge_duplicates(v))
}

/// Reference HTLC accounts of a claim or a refund: the caller (signer, writable),
/// the escrow PDA (writable), then the payee (the receiver for a claim, `refund_to`
/// for a refund, writable). For a token, the payee's associated token account takes
/// the payee's place, followed by the escrow's token account, the mint and the
/// token program.
pub fn spend_accounts(program: &Hash32, swap_id: &Hash32, caller: &Hash32, payee: &Hash32, token: Option<&TokenAccounts>) -> Option<Vec<AccountMeta>> {
    let escrow = escrow_address(program, swap_id)?;
    let mut v = vec![AccountMeta::signer_writable(*caller), AccountMeta::writable(escrow)];
    match token {
        None => v.push(AccountMeta::writable(*payee)),
        Some(t) => {
            v.push(AccountMeta::writable(associated_token_address(payee, t)?));
            v.push(AccountMeta::writable(associated_token_address(&escrow, t)?));
            v.push(AccountMeta::readonly(t.mint));
            v.push(AccountMeta::readonly(t.token_program));
        }
    }
    Some(merge_duplicates(v))
}

// ---------------------------------------------------------------------------
// Program and escrow identity (S7)
// ---------------------------------------------------------------------------

fn on_curve(bytes: &Hash32) -> bool {
    curve25519_dalek::edwards::CompressedEdwardsY(*bytes).decompress().is_some()
}

/// `create_program_address`: `None` when the hash lands on the curve.
pub fn create_program_address(seeds: &[&[u8]], program: &Hash32) -> Option<Hash32> {
    let mut data = Vec::new();
    for s in seeds {
        data.extend_from_slice(s);
    }
    data.extend_from_slice(program);
    data.extend_from_slice(b"ProgramDerivedAddress");
    let h = crate::sha256(&data);
    (!on_curve(&h)).then_some(h)
}

/// `find_program_address`: the first bump from 255 down that is off the curve.
pub fn find_program_address(seeds: &[&[u8]], program: &Hash32) -> Option<(Hash32, u8)> {
    (0..=255u8).rev().find_map(|bump| {
        let bump_seed = [bump];
        let mut all: Vec<&[u8]> = seeds.to_vec();
        all.push(&bump_seed);
        create_program_address(&all, program).map(|a| (a, bump))
    })
}

pub fn escrow_address(program: &Hash32, swap_id: &Hash32) -> Option<Hash32> {
    find_program_address(&[ESCROW_SEED, swap_id], program).map(|(a, _)| a)
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProgramPin {
    /// Base58 program id.
    pub program: String,
    /// `None`: the program must be immutable.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub upgrade_authority: Option<String>,
}

/// Observed facts about the program and the escrow account.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProgramFacts {
    pub executable: bool,
    pub upgrade_authority: Option<Hash32>,
    pub escrow_address: Hash32,
    pub escrow_owner: Hash32,
}

impl ProgramPin {
    /// S7 for Solana: the program is pinned and executable, its upgrade authority
    /// is none or the pinned account, and the escrow is the expected PDA owned by it.
    pub fn matches(&self, program: &Hash32, swap_id: &Hash32, facts: &ProgramFacts) -> bool {
        let Some(pinned) = parse_key(&self.program) else {
            return false;
        };
        let authority_ok = match (&self.upgrade_authority, facts.upgrade_authority) {
            (_, None) => true,
            (Some(a), Some(actual)) => parse_key(a) == Some(actual),
            (None, Some(_)) => false,
        };
        &pinned == program
            && facts.executable
            && authority_ok
            && facts.escrow_owner == *program
            && escrow_address(program, swap_id) == Some(facts.escrow_address)
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    fn compact(n: usize, out: &mut Vec<u8>) {
        let mut v = n;
        loop {
            let mut b = (v & 0x7f) as u8;
            v >>= 7;
            if v != 0 {
                b |= 0x80;
            }
            out.push(b);
            if v == 0 {
                break;
            }
        }
    }

    pub(crate) fn encode(keys: &[Hash32], ixs: &[(u8, Vec<u8>, Vec<u8>)], v0_lookups: Option<&[(Hash32, Vec<u8>, Vec<u8>)]>) -> Vec<u8> {
        let mut m = Vec::new();
        if v0_lookups.is_some() {
            m.push(0x80);
        }
        m.extend([1, 0, 1]);
        compact(keys.len(), &mut m);
        for k in keys {
            m.extend(k);
        }
        m.extend([9u8; 32]);
        compact(ixs.len(), &mut m);
        for (p, accounts, data) in ixs {
            m.push(*p);
            compact(accounts.len(), &mut m);
            m.extend(accounts);
            compact(data.len(), &mut m);
            m.extend(data);
        }
        if let Some(lookups) = v0_lookups {
            compact(lookups.len(), &mut m);
            for (k, w, r) in lookups {
                m.extend(k);
                compact(w.len(), &mut m);
                m.extend(w);
                compact(r.len(), &mut m);
                m.extend(r);
            }
        }
        m
    }

    const PAYER: Hash32 = [1; 32];
    const HTLC: Hash32 = [2; 32];

    fn base_keys() -> Vec<Hash32> {
        vec![PAYER, HTLC, key(COMPUTE_BUDGET_PROGRAM), key(SYSTEM_PROGRAM), key(ED25519_PROGRAM), key(TOKEN_PROGRAM), [3; 32], [4; 32], [5; 32]]
    }

    fn intent(mode: SolanaMode) -> SolanaIntent {
        SolanaIntent { htlc_program: HTLC, htlc_data: refund_data(&[7; 32]), htlc_accounts: vec![AccountMeta::signer_writable(PAYER)], fee_payer: PAYER, mode }
    }

    fn ed_data(pubkey: &Hash32, message: &[u8]) -> Vec<u8> {
        let mut d = vec![1u8, 0];
        let header = 16u16;
        let sig_off = header;
        let pk_off = sig_off + 64;
        let msg_off = pk_off + 32;
        for v in [sig_off, u16::MAX, pk_off, u16::MAX, msg_off, message.len() as u16, u16::MAX] {
            d.extend(v.to_le_bytes());
        }
        d.extend([0u8; 64]);
        d.extend(pubkey);
        d.extend(message);
        d
    }

    #[test]
    fn plain_refund() {
        let m = encode(&base_keys(), &[(2, vec![], vec![3, 0x40, 0x0d, 3, 0, 0, 0, 0, 0]), (1, vec![0], refund_data(&[7; 32]))], None);
        assert_eq!(check_message(&m, &LookupTables::new(), &intent(SolanaMode::default())).unwrap(), crate::blake3(&m));
    }

    #[test]
    fn rejections() {
        let refund = (1u8, vec![0u8], refund_data(&[7; 32]));
        let cases = vec![
            ("extra instruction", vec![refund.clone(), (6, vec![], vec![1])]),
            ("two HTLC calls", vec![refund.clone(), refund.clone()]),
            ("other HTLC data", vec![(1, vec![0], refund_data(&[8; 32]))]),
            ("system transfer", vec![(3, vec![0, 6], vec![2, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0]), refund.clone()]),
            ("nonce advance outside mode", vec![(3, vec![7, 6, 8], vec![4, 0, 0, 0]), refund.clone()]),
            ("ed25519 outside E2", vec![(4, vec![], ed_data(&[5; 32], b"m")), refund.clone()]),
            ("token transfer", vec![(5, vec![6, 7, 0], vec![3, 1, 0, 0, 0, 0, 0, 0, 0]), refund.clone()]),
            ("heap frame", vec![(2, vec![], vec![1, 0, 0, 1, 0]), refund.clone()]),
        ];
        for (name, ixs) in cases {
            let m = encode(&base_keys(), &ixs, None);
            assert!(check_message(&m, &LookupTables::new(), &intent(SolanaMode::default())).is_err(), "{name}");
        }
        // Foreign fee payer.
        let mut keys = base_keys();
        keys[0] = [9; 32];
        let m = encode(&keys, &[refund], None);
        assert!(check_message(&m, &LookupTables::new(), &intent(SolanaMode::default())).is_err());
    }

    #[test]
    fn durable_nonce_mode() {
        let mode = SolanaMode { durable_nonce: Some(NonceAccounts { nonce: [7; 32], authority: PAYER }), ed25519: None };
        let mut keys = base_keys();
        keys[6] = [7; 32];
        let advance = (3u8, vec![6u8, 8, 0], vec![4u8, 0, 0, 0]);
        let refund = (1u8, vec![0u8], refund_data(&[7; 32]));
        let ok = encode(&keys, &[advance.clone(), refund.clone()], None);
        assert!(check_message(&ok, &LookupTables::new(), &intent(mode.clone())).is_ok());
        let late = encode(&keys, &[refund.clone(), advance.clone()], None);
        assert!(check_message(&late, &LookupTables::new(), &intent(mode.clone())).is_err(), "advance not first");
        let missing = encode(&keys, &[refund.clone()], None);
        assert!(check_message(&missing, &LookupTables::new(), &intent(mode.clone())).is_err(), "no advance");
        let foreign = encode(&keys, &[(3, vec![7, 8, 0], vec![4, 0, 0, 0]), refund], None);
        assert!(check_message(&foreign, &LookupTables::new(), &intent(mode)).is_err(), "other nonce account");
    }

    #[test]
    fn ed25519_mode() {
        let mode = SolanaMode { durable_nonce: None, ed25519: Some(Ed25519Expect { pubkey: [5; 32], message: b"warrant".to_vec() }) };
        let refund = (1u8, vec![0u8], refund_data(&[7; 32]));
        let ok = encode(&base_keys(), &[(4, vec![], ed_data(&[5; 32], b"warrant")), refund.clone()], None);
        assert!(check_message(&ok, &LookupTables::new(), &intent(mode.clone())).is_ok());
        for bad in [ed_data(&[6; 32], b"warrant"), ed_data(&[5; 32], b"other")] {
            let m = encode(&base_keys(), &[(4, vec![], bad), refund.clone()], None);
            assert!(check_message(&m, &LookupTables::new(), &intent(mode.clone())).is_err());
        }
        let missing = encode(&base_keys(), &[refund], None);
        assert!(check_message(&missing, &LookupTables::new(), &intent(mode)).is_err());
    }

    #[test]
    fn version0_lookups() {
        // The HTLC program comes from a lookup table: index 9 is the first writable lookup.
        let keys = base_keys();
        let m = encode(&keys, &[(9, vec![0], refund_data(&[7; 32]))], Some(&[([8; 32], vec![1], vec![])]));
        assert!(parse_message(&m).unwrap().version0);
        let tables = |entries: Vec<Hash32>| LookupTables::from([([8; 32], entries)]);
        let none = LookupTables::new();
        assert_eq!(check_message(&m, &none, &intent(SolanaMode::default())), Err(SolError::LookupsUnresolved));
        assert!(check_message(&m, &tables(vec![[0xee; 32], HTLC]), &intent(SolanaMode::default())).is_ok());
        // The observed table resolves the index to another program, or has no such index.
        assert!(check_message(&m, &tables(vec![HTLC, [0xee; 32]]), &intent(SolanaMode::default())).is_err());
        assert_eq!(check_message(&m, &tables(vec![HTLC]), &intent(SolanaMode::default())), Err(SolError::LookupsUnresolved));
        // Another table than the message names.
        let other = LookupTables::from([([0x0a; 32], vec![[0xee; 32], HTLC])]);
        assert_eq!(check_message(&m, &other, &intent(SolanaMode::default())), Err(SolError::LookupsUnresolved));
    }

    #[test]
    fn htlc_accounts_and_privileges() {
        let escrow = escrow_address(&HTLC, &[7; 32]).unwrap();
        let payee = [5u8; 32];
        let mut intent = intent(SolanaMode::default());
        intent.htlc_accounts = spend_accounts(&HTLC, &[7; 32], &PAYER, &payee, None).unwrap();
        let keys = vec![PAYER, HTLC, escrow, payee, [0x0b; 32], key(SYSTEM_PROGRAM)];
        let tables = LookupTables::new();
        let ok = encode(&keys, &[(1, vec![0, 2, 3], refund_data(&[7; 32]))], None);
        assert!(check_message(&ok, &tables, &intent).is_ok());
        // Another payee, a missing account, an extra account.
        for accounts in [vec![0, 2, 4], vec![0, 2], vec![0, 2, 3, 4]] {
            let m = encode(&keys, &[(1, accounts, refund_data(&[7; 32]))], None);
            assert!(check_message(&m, &tables, &intent).is_err());
        }
        // The same accounts, but the header makes the payee read-only.
        let mut read_only = ok.clone();
        read_only[2] = 3;
        assert!(check_message(&read_only, &tables, &intent).is_err());
        // A key that is caller and payee is a signer at both places.
        assert_eq!(spend_accounts(&HTLC, &[7; 32], &PAYER, &PAYER, None).unwrap()[2], AccountMeta::signer_writable(PAYER));
        // A token lock: sender, escrow, both token accounts, mint, token program, System Program.
        let usdc = TokenAccounts { mint: [0x0c; 32], token_program: key(TOKEN_PROGRAM) };
        let lock = lock_accounts(&HTLC, &[7; 32], &PAYER, Some(&usdc)).unwrap();
        assert_eq!(lock.len(), 7);
        assert_eq!(lock[3], AccountMeta::writable(associated_token_address(&escrow, &usdc).unwrap()));
        assert_eq!(lock[6], AccountMeta::readonly(key(SYSTEM_PROGRAM)));
        // Header counts outside their groups.
        assert!(parse_message(&encode(&keys, &[], None).iter().enumerate().map(|(i, b)| if i == 1 { 1 } else { *b }).collect::<Vec<_>>()).is_err());
    }

    #[test]
    fn malformed() {
        assert!(parse_message(&[]).is_err());
        assert!(parse_message(&[0x81, 1, 0, 0]).is_err(), "version 1");
        let m = encode(&base_keys(), &[(1, vec![0], refund_data(&[7; 32]))], None);
        assert!(parse_message(&m[..m.len() - 1]).is_err());
        let mut extra = m.clone();
        extra.push(0);
        assert!(parse_message(&extra).is_err());
        // Non-canonical compact length: 0x80 0x00 encodes 0.
        let mut r = Reader { data: &[0x80, 0x00], pos: 0 };
        assert!(r.compact().is_err());
    }

    /// Cross-check against the Solana SDK's `find_program_address`.
    #[test]
    fn pda_derivation_matches_solana_sdk() {
        use solana_pubkey::Pubkey;
        let wallet = parse_key("4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T").unwrap();
        let mint = parse_key("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v").unwrap();
        let seeds: [&[u8]; 3] = [&wallet, &key(TOKEN_PROGRAM), &mint];
        let (ours, our_bump) = find_program_address(&seeds, &key(ATA_PROGRAM)).unwrap();
        let (theirs, their_bump) = Pubkey::find_program_address(&seeds, &Pubkey::new_from_array(key(ATA_PROGRAM)));
        assert_eq!((ours, our_bump), (theirs.to_bytes(), their_bump));
        let usdc = TokenAccounts { mint, token_program: key(TOKEN_PROGRAM) };
        assert_eq!(associated_token_address(&wallet, &usdc), Some(ours));
        for id in [[7u8; 32], [8; 32], [0; 32]] {
            let (theirs, _) = Pubkey::find_program_address(&[ESCROW_SEED, &id], &Pubkey::new_from_array(HTLC));
            assert_eq!(escrow_address(&HTLC, &id), Some(theirs.to_bytes()));
        }
    }

    #[test]
    fn program_pins() {
        let program = key(TOKEN_PROGRAM);
        let pin = ProgramPin { program: TOKEN_PROGRAM.into(), upgrade_authority: None };
        let facts = ProgramFacts {
            executable: true,
            upgrade_authority: None,
            escrow_address: escrow_address(&program, &[7; 32]).unwrap(),
            escrow_owner: program,
        };
        assert!(pin.matches(&program, &[7; 32], &facts));
        assert!(!pin.matches(&program, &[8; 32], &facts), "escrow of another swap");
        assert!(!pin.matches(&program, &[7; 32], &ProgramFacts { upgrade_authority: Some([1; 32]), ..facts.clone() }), "upgradeable");
        assert!(!pin.matches(&program, &[7; 32], &ProgramFacts { escrow_owner: [3; 32], ..facts.clone() }), "foreign owner");
        assert!(!pin.matches(&key(ATA_PROGRAM), &[7; 32], &facts), "other program");
    }
}
