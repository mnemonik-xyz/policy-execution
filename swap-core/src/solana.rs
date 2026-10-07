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
/// The upgradeable BPF loader (loader v3). S7 accepts a program under this loader only.
pub const BPF_LOADER_UPGRADEABLE: &str = "BPFLoaderUpgradeab1e11111111111111111111111";

/// `UpgradeableLoaderState::size_of_programdata_metadata()`: the enum tag (4 bytes),
/// the deployment slot (8) and the upgrade authority as `Option<Pubkey>` (1 + 32).
/// The program bytes start after it.
pub const PROGRAMDATA_HEADER_LEN: usize = 45;

/// PDA seed prefix of the reference HTLC escrow: `[b"htlc", lock_id]` (spec 8.2).
pub const ESCROW_SEED: &[u8] = b"htlc";

/// The first 8 data bytes of a reference HTLC escrow account:
/// `sha256("account:Escrow")[..8]` (implementation spec 4.6).
pub const ESCROW_DISCRIMINATOR: [u8; 8] = [0x1f, 0xd5, 0x7b, 0xbb, 0xba, 0x16, 0xda, 0x9b];

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
/// `amount` is the gross debit. The data carries `swap_id` and the leg: the program
/// derives `lock_id = sha256(swap_id ‖ leg ‖ sender)` from them and the signing
/// sender, and requires the escrow account at `[b"htlc", lock_id]`.
pub struct LockData {
    pub swap_id: Hash32,
    pub leg: crate::types::LegName,
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
        d.push(self.leg.lock_byte());
        d.extend(self.receiver);
        d.extend(self.refund_to);
        d.extend(self.mint);
        d.extend(self.amount.to_le_bytes());
        d.extend(self.hashlock);
        d.extend(self.timelock.to_le_bytes());
        d
    }
}

pub fn claim_data(lock_id: &Hash32, preimage: &Hash32) -> Vec<u8> {
    let mut d = vec![1u8];
    d.extend(lock_id);
    d.extend(preimage);
    d
}

pub fn refund_data(lock_id: &Hash32) -> Vec<u8> {
    let mut d = vec![2u8];
    d.extend(lock_id);
    d
}

/// The two token programs that the reference HTLC and the decoder accept.
pub fn is_token_program(program: &Hash32) -> bool {
    *program == key(TOKEN_PROGRAM) || *program == key(TOKEN_2022_PROGRAM)
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
pub fn lock_accounts(program: &Hash32, lock_id: &Hash32, sender: &Hash32, token: Option<&TokenAccounts>) -> Option<Vec<AccountMeta>> {
    let escrow = escrow_address(program, lock_id)?;
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
pub fn spend_accounts(program: &Hash32, lock_id: &Hash32, caller: &Hash32, payee: &Hash32, token: Option<&TokenAccounts>) -> Option<Vec<AccountMeta>> {
    let escrow = escrow_address(program, lock_id)?;
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

/// The escrow PDA of the lock with key `lock_id`: seeds `[b"htlc", lock_id]`.
pub fn escrow_address(program: &Hash32, lock_id: &Hash32) -> Option<Hash32> {
    find_program_address(&[ESCROW_SEED, lock_id], program).map(|(a, _)| a)
}

/// The escrow token account of a token lock: the associated token account of the
/// escrow PDA for the leg mint (implementation spec 4.6).
pub fn escrow_token_address(program: &Hash32, lock_id: &Hash32, token: &TokenAccounts) -> Option<Hash32> {
    escrow_address(program, lock_id).and_then(|e| associated_token_address(&e, token))
}

/// The ProgramData address of an upgradeable program: the loader PDA of the seed
/// `[program]`.
pub fn programdata_address(program: &Hash32) -> Option<Hash32> {
    find_program_address(&[program], &key(BPF_LOADER_UPGRADEABLE)).map(|(a, _)| a)
}

/// The ProgramData address in the data of an upgradeable program account: the
/// bincode `UpgradeableLoaderState::Program { programdata_address }`, tag 2 as a
/// little-endian u32 and then 32 bytes. Bytes after them are ignored, as the loader
/// ignores them. `None` for any other state.
pub fn decode_program_account(data: &[u8]) -> Option<Hash32> {
    if data.get(..4)? != [2, 0, 0, 0] {
        return None;
    }
    data.get(4..36)?.try_into().ok()
}

/// The upgrade authority and the code hash of a ProgramData account: the bincode
/// `UpgradeableLoaderState::ProgramData { slot, upgrade_authority_address }`, tag 3,
/// the slot, then the option byte (0 none, 1 some) and the 32-byte key, then the
/// program bytes from `PROGRAMDATA_HEADER_LEN`. After a 0 option byte the 32 key
/// bytes can still hold an old authority; they are ignored. `None` for any other
/// state or an option byte other than 0 or 1.
pub fn decode_programdata(data: &[u8]) -> Option<(Option<Hash32>, Hash32)> {
    if data.len() < PROGRAMDATA_HEADER_LEN || data[..4] != [3, 0, 0, 0] {
        return None;
    }
    let authority = match data[12] {
        0 => None,
        1 => Some(data[13..PROGRAMDATA_HEADER_LEN].try_into().ok()?),
        _ => return None,
    };
    Some((authority, code_hash(&data[PROGRAMDATA_HEADER_LEN..])))
}

/// The code hash of program bytes (spec 8.4): SHA-256 of the bytes without their
/// trailing zero bytes. Over the bytes after the ProgramData header it is the value
/// of `solana-verify get-program-hash`; over a built `.so` file, the value of
/// `solana-verify get-executable-hash`.
pub fn code_hash(program_bytes: &[u8]) -> Hash32 {
    let end = program_bytes.iter().rposition(|b| *b != 0).map_or(0, |i| i + 1);
    crate::sha256(&program_bytes[..end])
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProgramPin {
    /// Base58 program id.
    pub program: String,
    /// `code_hash` of the program bytes in its ProgramData account: the value of
    /// `solana-verify get-program-hash`, as hex.
    #[serde(with = "crate::enc::hex32")]
    pub code_hash: Hash32,
    /// `None`: the program must be immutable.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub upgrade_authority: Option<String>,
}

/// The account at the escrow address, as read from the chain.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EscrowAccount {
    pub owner: Hash32,
    /// The length of the account data in bytes.
    pub data_len: u64,
    /// The first 8 bytes of the account data. `None` when the data is shorter.
    pub discriminator: Option<[u8; 8]>,
}

/// The ProgramData account of an upgradeable program, as read from the chain and
/// decoded by `decode_programdata`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProgramDataAccount {
    /// The owner of the account (the loader).
    pub owner: Hash32,
    pub upgrade_authority: Option<Hash32>,
    /// `code_hash` of the bytes after the header.
    pub code_hash: Hash32,
}

/// A token account as read from the chain: the base layout that SPL Token and
/// Token-2022 share.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TokenAccount {
    /// The address that the account was read from.
    pub address: Hash32,
    /// The owner of the account (the token program).
    pub program: Hash32,
    pub mint: Hash32,
    /// The owner field of the token account.
    pub owner: Hash32,
    /// The state is `Initialized` or `Frozen`. S27 reads whether it is frozen.
    pub initialized: bool,
}

/// What a Solana escrow holds: native SOL in the escrow PDA, or a token in the
/// escrow token account.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EscrowAsset {
    Native,
    /// The leg mint and the program that owns it, read from the chain (S9).
    Token(TokenAccounts),
}

/// Observed facts about the program and the escrow account.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProgramFacts {
    pub executable: bool,
    /// The owner of the program account: the loader that runs the program.
    pub loader: Hash32,
    /// The ProgramData address in the program account data
    /// (`decode_program_account`). `None`: the data is not a `Program` state.
    pub programdata_address: Option<Hash32>,
    /// The account at `programdata_address`. `None`: no account exists there, or its
    /// data is not a `ProgramData` state (`decode_programdata`).
    pub programdata: Option<ProgramDataAccount>,
    /// The address that the escrow account was read from.
    pub escrow_address: Hash32,
    /// The account at `escrow_address`. `None`: no account exists there.
    pub escrow: Option<EscrowAccount>,
    /// A token leg: the account at `escrow_token_address`. `None`: no account exists
    /// there, or it is not a token account of SPL Token or Token-2022. Not read for
    /// a native leg.
    pub escrow_token: Option<TokenAccount>,
}

impl ProgramFacts {
    /// The escrow account is in the state that the action needs. `locked`: the
    /// lock exists now, so the program owns the escrow account and its data starts
    /// with `ESCROW_DISCRIMINATOR`. Before the own lock, the address holds no
    /// account, or only lamports: a System-owned account without data, which the
    /// lock instruction can still take (anyone can send lamports to an address).
    pub fn escrow_ready(&self, program: &Hash32, locked: bool) -> bool {
        match (&self.escrow, locked) {
            (Some(e), true) => e.owner == *program && e.discriminator == Some(ESCROW_DISCRIMINATOR),
            (None, false) => true,
            (Some(e), false) => e.owner == key(SYSTEM_PROGRAM) && e.data_len == 0,
            (None, true) => false,
        }
    }

    /// The escrow token account of a token leg is in the state that the action
    /// needs. Once the lock exists (`locked`), the account at `escrow_token_address`
    /// is a token account of the leg's token program, with the leg mint and the
    /// escrow PDA as owner. Before the own lock nothing is required: the lock
    /// instruction creates or takes the account, and S24 binds its address. A native
    /// leg has no escrow token account.
    pub fn escrow_token_ready(&self, program: &Hash32, lock_id: &Hash32, asset: &EscrowAsset, locked: bool) -> bool {
        let EscrowAsset::Token(token) = asset else {
            return true;
        };
        if !locked {
            return true;
        }
        let (Some(escrow), Some(t)) = (escrow_address(program, lock_id), &self.escrow_token) else {
            return false;
        };
        escrow_token_address(program, lock_id, token) == Some(t.address)
            && t.program == token.token_program
            && t.mint == token.mint
            && t.owner == escrow
            && t.initialized
    }
}

impl ProgramPin {
    /// The program id and the upgrade authority, if any, are base58 keys.
    pub fn well_formed(&self) -> bool {
        parse_key(&self.program).is_some() && self.upgrade_authority.as_deref().is_none_or(|a| parse_key(a).is_some())
    }

    /// S7 for Solana (spec 8.4): the program is pinned and executable under the
    /// upgradeable loader. Its program account names the ProgramData PDA, which the
    /// loader owns; the upgrade authority there is none or the pinned account, and
    /// the code hash is the pinned hash. The escrow address is the expected PDA,
    /// and the escrow account (`ProgramFacts::escrow_ready`) and, for a token leg,
    /// the escrow token account (`ProgramFacts::escrow_token_ready`) are in the state
    /// that the action needs. `locked`: the lock exists now.
    pub fn matches(&self, program: &Hash32, lock_id: &Hash32, asset: &EscrowAsset, facts: &ProgramFacts, locked: bool) -> bool {
        let Some(pinned) = parse_key(&self.program) else {
            return false;
        };
        let loader = key(BPF_LOADER_UPGRADEABLE);
        let programdata_ok = facts.programdata.as_ref().is_some_and(|d| {
            let authority_ok = match (&self.upgrade_authority, d.upgrade_authority) {
                (_, None) => true,
                (Some(a), Some(actual)) => parse_key(a) == Some(actual),
                (None, Some(_)) => false,
            };
            d.owner == loader && d.code_hash == self.code_hash && authority_ok
        });
        &pinned == program
            && facts.executable
            && facts.loader == loader
            && programdata_address(program).is_some_and(|a| facts.programdata_address == Some(a))
            && programdata_ok
            && facts.escrow_ready(program, locked)
            && escrow_address(program, lock_id) == Some(facts.escrow_address)
            && facts.escrow_token_ready(program, lock_id, asset, locked)
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
        // The ProgramData PDA of the upgradeable loader.
        let loader = Pubkey::new_from_array(key(BPF_LOADER_UPGRADEABLE));
        assert_eq!(loader.to_string(), BPF_LOADER_UPGRADEABLE);
        for program in [HTLC, wallet] {
            let (theirs, _) = Pubkey::find_program_address(&[&program], &loader);
            assert_eq!(programdata_address(&program), Some(theirs.to_bytes()));
        }
    }

    /// Program bytes of a test HTLC: an ELF prefix and trailing zero bytes, as in a
    /// ProgramData account with spare room.
    pub(crate) const HTLC_CODE: &[u8] = b"\x7fELF\x02\x01\x01\x00reference htlc\x00\x00\x00";

    pub(crate) fn htlc_pin(program: &Hash32) -> ProgramPin {
        ProgramPin { program: bs58::encode(program).into_string(), code_hash: code_hash(HTLC_CODE), upgrade_authority: None }
    }

    /// Facts of an immutable program under the upgradeable loader with `HTLC_CODE`.
    pub(crate) fn htlc_facts(program: &Hash32, lock_id: &Hash32, escrow: Option<EscrowAccount>) -> ProgramFacts {
        let loader = key(BPF_LOADER_UPGRADEABLE);
        ProgramFacts {
            executable: true,
            loader,
            programdata_address: programdata_address(program),
            programdata: Some(ProgramDataAccount { owner: loader, upgrade_authority: None, code_hash: code_hash(HTLC_CODE) }),
            escrow_address: escrow_address(program, lock_id).unwrap(),
            escrow,
            escrow_token: None,
        }
    }

    pub(crate) fn locked_escrow(program: &Hash32) -> EscrowAccount {
        EscrowAccount { owner: *program, data_len: 113, discriminator: Some(ESCROW_DISCRIMINATOR) }
    }

    /// Spec 3.2 and 8.2: the reference lock data carries `swap_id` and the leg byte
    /// after the tag, in the order of the EVM ABI; claim and refund carry `lock_id`.
    #[test]
    fn htlc_data_layout() {
        use crate::types::LegName;
        let d = LockData { swap_id: [1; 32], leg: LegName::A, receiver: [2; 32], refund_to: [3; 32], mint: [0; 32], amount: 5, hashlock: [4; 32], timelock: 6 }.encode();
        assert_eq!(d.len(), 1 + 32 + 1 + 32 * 3 + 8 + 32 + 8);
        assert_eq!((d[0], &d[1..33], d[33], &d[34..66]), (0, &[1u8; 32][..], 0x41, &[2u8; 32][..]));
        assert_eq!(&claim_data(&[7; 32], &[8; 32])[1..33], &[7; 32]);
        assert_eq!(refund_data(&[7; 32]), [&[2u8][..], &[7; 32]].concat());
    }

    #[test]
    fn program_pins() {
        let program = HTLC;
        let pin = htlc_pin(&program);
        let facts = htlc_facts(&program, &[7; 32], Some(locked_escrow(&program)));
        let native = EscrowAsset::Native;
        let ok = |f: &ProgramFacts| pin.matches(&program, &[7; 32], &native, f, true);
        assert!(ok(&facts));
        assert!(!pin.matches(&program, &[8; 32], &native, &facts, true), "escrow of another swap");
        assert!(!pin.matches(&[3; 32], &[7; 32], &native, &facts, true), "other program");
        let foreign = EscrowAccount { owner: [3; 32], ..locked_escrow(&program) };
        assert!(!ok(&ProgramFacts { escrow: Some(foreign), ..facts.clone() }), "foreign escrow owner");
        assert!(!ok(&ProgramFacts { executable: false, ..facts.clone() }), "not executable");
        let data = facts.programdata.clone().unwrap();
        let with_data = |d: ProgramDataAccount| ProgramFacts { programdata: Some(d), ..facts.clone() };
        assert!(!ok(&with_data(ProgramDataAccount { upgrade_authority: Some([1; 32]), ..data.clone() })), "upgradeable");
        assert!(!ok(&with_data(ProgramDataAccount { code_hash: code_hash(b"\x7fELF other"), ..data.clone() })), "other code");
        assert!(!ok(&with_data(ProgramDataAccount { owner: [3; 32], ..data.clone() })), "ProgramData of another owner");
        assert!(!ok(&ProgramFacts { programdata: None, ..facts.clone() }), "no ProgramData account");
        // The pinned authority may upgrade; an immutable program also passes that pin.
        let pinned = ProgramPin { upgrade_authority: Some(bs58::encode([1u8; 32]).into_string()), ..pin.clone() };
        let upgradeable = with_data(ProgramDataAccount { upgrade_authority: Some([1; 32]), ..data.clone() });
        assert!(pinned.matches(&program, &[7; 32], &native, &upgradeable, true));
        assert!(pinned.matches(&program, &[7; 32], &native, &facts, true));
        let other = with_data(ProgramDataAccount { upgrade_authority: Some([4; 32]), ..data });
        assert!(!pinned.matches(&program, &[7; 32], &native, &other, true), "another authority");
    }

    /// Spec 8.4 Solana (G21): only the upgradeable loader, with the program account
    /// naming the ProgramData PDA of the program.
    #[test]
    fn program_loader() {
        let program = HTLC;
        let pin = htlc_pin(&program);
        let facts = htlc_facts(&program, &[7; 32], Some(locked_escrow(&program)));
        let ok = |f: &ProgramFacts| pin.matches(&program, &[7; 32], &EscrowAsset::Native, f, true);
        assert!(ok(&facts));
        for loader in ["BPFLoader2111111111111111111111111111111111", "BPFLoader1111111111111111111111111111111111", "LoaderV411111111111111111111111111111111111", "NativeLoader1111111111111111111111111111111"] {
            assert!(!ok(&ProgramFacts { loader: key(loader), ..facts.clone() }), "{loader}");
        }
        assert!(!ok(&ProgramFacts { programdata_address: None, ..facts.clone() }), "program account is not a Program state");
        let other = programdata_address(&[3; 32]);
        assert!(!ok(&ProgramFacts { programdata_address: other, ..facts.clone() }), "ProgramData of another program");
    }

    /// Spec 8.4 Solana (G21): once a token lock exists, the escrow token account is
    /// the escrow PDA's account for the leg mint under the leg's token program.
    #[test]
    fn escrow_token_account() {
        let program = HTLC;
        let pin = htlc_pin(&program);
        let usdc = TokenAccounts { mint: [0x0c; 32], token_program: key(TOKEN_PROGRAM) };
        let token = EscrowAsset::Token(usdc);
        let escrow = escrow_address(&program, &[7; 32]).unwrap();
        let account = TokenAccount { address: escrow_token_address(&program, &[7; 32], &usdc).unwrap(), program: usdc.token_program, mint: usdc.mint, owner: escrow, initialized: true };
        assert_eq!(account.address, associated_token_address(&escrow, &usdc).unwrap());
        let facts = |t: Option<TokenAccount>| ProgramFacts { escrow_token: t, ..htlc_facts(&program, &[7; 32], Some(locked_escrow(&program))) };
        assert!(pin.matches(&program, &[7; 32], &token, &facts(Some(account.clone())), true));
        let t22 = TokenAccounts { token_program: key(TOKEN_2022_PROGRAM), ..usdc };
        let bad = [
            ("no account", None),
            ("other address", Some(TokenAccount { address: associated_token_address(&[9; 32], &usdc).unwrap(), ..account.clone() })),
            ("other token program", Some(TokenAccount { program: key(TOKEN_2022_PROGRAM), ..account.clone() })),
            ("account of the other token program", Some(TokenAccount { address: associated_token_address(&escrow, &t22).unwrap(), program: t22.token_program, ..account.clone() })),
            ("other mint", Some(TokenAccount { mint: [0x0d; 32], ..account.clone() })),
            ("other owner", Some(TokenAccount { owner: [9; 32], ..account.clone() })),
            ("uninitialized", Some(TokenAccount { initialized: false, ..account.clone() })),
        ];
        for (name, t) in bad {
            assert!(!pin.matches(&program, &[7; 32], &token, &facts(t), true), "{name}");
        }
        // A Token-2022 mint: the escrow account for that mint under Token-2022 passes.
        let t22_account = TokenAccount { address: escrow_token_address(&program, &[7; 32], &t22).unwrap(), program: t22.token_program, ..account.clone() };
        assert!(pin.matches(&program, &[7; 32], &EscrowAsset::Token(t22), &facts(Some(t22_account)), true));
        // The escrow of another swap has another token account.
        assert!(!facts(Some(account.clone())).escrow_token_ready(&program, &[8; 32], &token, true), "another swap");
        // A native leg has none; before the own lock nothing is read.
        assert!(pin.matches(&program, &[7; 32], &EscrowAsset::Native, &facts(None), true));
        let before = |t: Option<TokenAccount>| ProgramFacts { escrow: None, ..facts(t) };
        assert!(pin.matches(&program, &[7; 32], &token, &before(None), false));
        assert!(pin.matches(&program, &[7; 32], &token, &before(Some(TokenAccount { owner: [9; 32], ..account })), false));
    }

    /// Spec 8.4 Solana (D3): a lock that exists has a program-owned escrow with the
    /// reference discriminator. Before the own lock, the escrow PDA holds no account
    /// or only lamports.
    #[test]
    fn escrow_account_state() {
        let program = HTLC;
        let pin = htlc_pin(&program);
        let escrow = locked_escrow(&program);
        let facts = htlc_facts(&program, &[7; 32], Some(escrow.clone()));
        let with = |e: Option<EscrowAccount>| ProgramFacts { escrow: e, ..facts.clone() };
        let m = |swap_id: &Hash32, f: &ProgramFacts, locked: bool| pin.matches(&program, swap_id, &EscrowAsset::Native, f, locked);
        // A lock that exists.
        assert!(m(&[7; 32], &facts, true));
        let other = EscrowAccount { discriminator: Some([0; 8]), ..escrow.clone() };
        assert!(!m(&[7; 32], &with(Some(other)), true), "another account type");
        let closed = EscrowAccount { discriminator: Some([0xff; 8]), ..escrow.clone() };
        assert!(!m(&[7; 32], &with(Some(closed)), true), "closed account");
        let short = EscrowAccount { data_len: 7, discriminator: None, ..escrow.clone() };
        assert!(!m(&[7; 32], &with(Some(short)), true), "data shorter than 8 bytes");
        assert!(!m(&[7; 32], &with(None), true), "no escrow account");
        // Before the own lock.
        assert!(m(&[7; 32], &with(None), false));
        assert!(!m(&[7; 32], &facts, false), "escrow exists before the lock");
        let funded = EscrowAccount { owner: key(SYSTEM_PROGRAM), data_len: 0, discriminator: None };
        assert!(m(&[7; 32], &with(Some(funded.clone())), false), "lamports only: the lock can take it");
        assert!(!m(&[7; 32], &with(Some(funded.clone())), true), "lamports only is not a lock");
        let allocated = EscrowAccount { data_len: 1, ..funded.clone() };
        assert!(!m(&[7; 32], &with(Some(allocated)), false), "System-owned account with data");
        let foreign = EscrowAccount { owner: [3; 32], ..funded };
        assert!(!m(&[7; 32], &with(Some(foreign)), false), "account of another owner");
        assert!(!m(&[8; 32], &with(None), false), "address of another swap");
        let mut upgradeable = with(None);
        upgradeable.programdata.as_mut().unwrap().upgrade_authority = Some([1; 32]);
        assert!(!m(&[7; 32], &upgradeable, false), "program checks still apply");
        assert!(!m(&[7; 32], &ProgramFacts { loader: [3; 32], ..with(None) }, false), "loader checks still apply");
    }

    /// Spec 8.4 Solana (G21): the loader account layouts and the code hash, which
    /// `solana-verify` computes as SHA-256 of the program bytes without trailing zeros.
    #[test]
    fn upgradeable_loader_decoders() {
        // sha256("abc"), FIPS 180-2.
        let abc = crate::from_hex_array::<32>("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad").unwrap();
        assert_eq!(code_hash(b"abc"), abc);
        assert_eq!(code_hash(b"abc\0\0\0"), abc, "trailing zeros are padding");
        assert_ne!(code_hash(b"a\0bc"), code_hash(b"abc"), "inner zeros count");
        assert_eq!(code_hash(&[0; 9]), crate::sha256(b""));
        // Program account: tag 2 and the ProgramData address.
        let mut program_account = vec![2, 0, 0, 0];
        program_account.extend([5u8; 32]);
        assert_eq!(decode_program_account(&program_account), Some([5; 32]));
        let mut longer = program_account.clone();
        longer.push(0);
        assert_eq!(decode_program_account(&longer), Some([5; 32]), "bytes after the state are ignored");
        assert_eq!(decode_program_account(&program_account[..35]), None);
        for tag in [[0, 0, 0, 0], [1, 0, 0, 0], [3, 0, 0, 0], [2, 0, 0, 1]] {
            let mut d = program_account.clone();
            d[..4].copy_from_slice(&tag);
            assert_eq!(decode_program_account(&d), None, "{tag:?}");
        }
        // ProgramData: tag 3, slot, authority option, program bytes from offset 45.
        let header = |option: u8, key: [u8; 32]| {
            let mut d = vec![3, 0, 0, 0];
            d.extend(300_000_000u64.to_le_bytes());
            d.push(option);
            d.extend(key);
            d
        };
        let with_code = |mut d: Vec<u8>| {
            d.extend(HTLC_CODE);
            d.extend([0u8; 64]);
            d
        };
        assert_eq!(header(0, [0; 32]).len(), PROGRAMDATA_HEADER_LEN);
        assert_eq!(decode_programdata(&with_code(header(1, [6; 32]))), Some((Some([6; 32]), code_hash(HTLC_CODE))));
        assert_eq!(decode_programdata(&with_code(header(0, [6; 32]))), Some((None, code_hash(HTLC_CODE))), "stale key after none");
        assert_eq!(decode_programdata(&header(0, [0; 32])), Some((None, crate::sha256(b""))));
        assert_eq!(decode_programdata(&with_code(header(2, [6; 32]))), None, "bad option byte");
        assert_eq!(decode_programdata(&header(1, [6; 32])[..44]), None, "short header");
        let mut buffer = with_code(header(1, [6; 32]));
        buffer[0] = 1;
        assert_eq!(decode_programdata(&buffer), None, "a Buffer state");
        assert_eq!(decode_programdata(&program_account), None);
    }

    /// Cross-check of the loader layouts against the Solana SDK (bincode
    /// `UpgradeableLoaderState`).
    #[test]
    fn upgradeable_loader_matches_solana_sdk() {
        use solana_loader_v3_interface::state::UpgradeableLoaderState;
        use solana_pubkey::Pubkey;
        assert_eq!(UpgradeableLoaderState::size_of_programdata_metadata(), PROGRAMDATA_HEADER_LEN);
        assert_eq!(solana_sdk_ids::bpf_loader_upgradeable::id().to_bytes(), key(BPF_LOADER_UPGRADEABLE));
        let program = UpgradeableLoaderState::Program { programdata_address: Pubkey::new_from_array([5; 32]) };
        assert_eq!(decode_program_account(&bincode::serialize(&program).unwrap()), Some([5; 32]));
        for authority in [None, Some(Pubkey::new_from_array([6; 32]))] {
            let state = UpgradeableLoaderState::ProgramData { slot: 300_000_000, upgrade_authority_address: authority };
            let mut data = vec![0u8; PROGRAMDATA_HEADER_LEN];
            bincode::serialize_into(&mut data[..], &state).unwrap();
            data.extend(HTLC_CODE);
            assert_eq!(decode_programdata(&data), Some((authority.map(|a| a.to_bytes()), code_hash(HTLC_CODE))));
        }
        let buffer = UpgradeableLoaderState::Buffer { authority_address: Some(Pubkey::new_from_array([6; 32])) };
        let mut data = vec![0u8; PROGRAMDATA_HEADER_LEN];
        bincode::serialize_into(&mut data[..], &buffer).unwrap();
        assert_eq!(decode_programdata(&data), None);
        assert_eq!(decode_program_account(&data), None);
    }

    #[test]
    fn escrow_discriminator_is_the_anchor_account_hash() {
        assert_eq!(crate::sha256(b"account:Escrow")[..8], ESCROW_DISCRIMINATOR);
    }
}
