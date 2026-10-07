//! S24: the agent proposes a transaction; the signer derives the intent from the
//! terms and accepts the transaction only if it does the authorized action and
//! nothing more (spec 4.2). The result is the warrant's transaction binding.

use crate::bitcoin::{self, Leaf};
use crate::caip::Family;
use crate::evm::{self, EvmIntent, LockCall};
use crate::facts::TransferFee;
use crate::solana::{self, LockData, LookupTables, SolanaIntent, SolanaMode, TokenAccounts};
use crate::types::{Action, Leg, TimelockSpec};
use crate::warrant::TxBinding;
use crate::Hash32;

/// A proposed transaction in its chain's unsigned form.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ProposedTx {
    /// PSBT version 0.
    BitcoinPsbt(Vec<u8>),
    /// Unsigned EIP-1559 transactions: `[approve, lock]` for a token lock, else one.
    Evm(Vec<Vec<u8>>),
    /// A legacy or version 0 message. Lookup tables come from chain facts
    /// (`BindContext::lookup_tables`), never from the proposal.
    Solana { message: Vec<u8> },
}

/// The signer's own accounts.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct OwnAccounts {
    /// CAIP-10 accounts that receive claims and refunds (S5, S6).
    pub accounts: Vec<crate::caip::AccountId>,
    /// Own Taproot `scriptPubKey`s: inputs and change on Bitcoin.
    pub bitcoin_scripts: Vec<Vec<u8>>,
    /// Own x-only keys that claim or refund a Bitcoin HTLC (S5, S6). On Bitcoin the
    /// key in the leaf, not the CAIP-10 account, decides who can spend.
    pub bitcoin_keys: Vec<Hash32>,
    /// Fee payer on Solana.
    pub solana_fee_payer: Option<Hash32>,
}

/// Chain facts that a binding needs.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct BindContext {
    /// Transfer fee of the leg asset, read from the chain (spec 4.2 as fixed).
    pub transfer_fee: Option<TransferFee>,
    /// The observed HTLC output on Bitcoin, for claims and refunds.
    pub htlc_outpoint: Option<(Hash32, u32)>,
    /// The preimage for a reveal or a claim.
    pub preimage: Option<Hash32>,
    pub solana_mode: SolanaMode,
    /// The highest fee the transaction may pay, in base units of the native coin
    /// (the profile's worst-case fee of the action). Checked on Bitcoin; 0 allows none.
    pub max_fee: u128,
    /// Solana: the address lookup tables of the leg chain, read from the chain.
    pub lookup_tables: LookupTables,
    /// Solana token leg: the program that owns the mint, read from the chain.
    pub token_program: Option<Hash32>,
    /// Bitcoin: the observed tip height of the leg chain, for `nLockTime` checks.
    pub tip_height: Option<u64>,
}

fn evm_addr(account: &crate::caip::AccountId) -> Result<[u8; 20], String> {
    account.evm_address().ok_or_else(|| format!("{account} is not an EVM account"))
}

fn sol_key(account: &crate::caip::AccountId) -> Result<Hash32, String> {
    account.solana_key().ok_or_else(|| format!("{account} is not a Solana account"))
}

fn unix_time(lock: &crate::types::Lock) -> Result<u64, String> {
    match lock.timelock {
        TimelockSpec::Time(t) => Ok(t),
        _ => Err("the reference HTLC on this chain takes an absolute time".into()),
    }
}

/// The gross debit that puts exactly the leg amount into the lock.
pub fn gross_debit(leg: &Leg, fee: Option<&TransferFee>) -> Result<u128, String> {
    match fee {
        None => Ok(leg.amount),
        Some(f) => f.gross_for_net(leg.amount).ok_or_else(|| "no gross debit gives the agreed net amount".into()),
    }
}

/// Check `tx` against the intent of `action` on `leg` and return the binding.
pub fn bind(action: Action, leg: &Leg, tx: &ProposedTx, own: &OwnAccounts, ctx: &BindContext) -> Result<TxBinding, String> {
    let family = leg.chain.family().ok_or("unsupported chain")?;
    let lock = &leg.lock;
    let preimage = || ctx.preimage.ok_or_else(|| "a claim needs the preimage".to_string());
    match (family, tx) {
        (Family::Bitcoin, ProposedTx::BitcoinPsbt(bytes)) => {
            let psbt = bitcoin::parse_psbt(bytes).map_err(|e| e.to_string())?;
            let max_fee = u64::try_from(ctx.max_fee).unwrap_or(u64::MAX);
            let binding = match action {
                Action::Lock => {
                    let amount = u64::try_from(leg.amount).map_err(|_| "amount exceeds the Bitcoin range")?;
                    bitcoin::check_lock_psbt(&psbt, lock, amount, &own.bitcoin_scripts, max_fee, ctx.tip_height)
                }
                Action::Reveal | Action::Claim | Action::Refund => {
                    let (txid, vout) = ctx.htlc_outpoint.ok_or("the HTLC output is not observed")?;
                    let leaf = if action == Action::Refund { Leaf::Refund } else { Leaf::Claim };
                    bitcoin::check_spend_psbt(&psbt, lock, (&txid, vout), leaf, &own.bitcoin_scripts, max_fee, ctx.tip_height)
                }
                Action::Accept => return Err("accept has no transaction".into()),
            }
            .map_err(|e| e.to_string())?;
            Ok(TxBinding::Bitcoin { txid: binding.txid, sighashes: binding.sighashes, sighash_types: binding.sighash_types })
        }
        (Family::Evm, ProposedTx::Evm(txs)) => {
            let chain_id = leg.chain.evm_chain_id().ok_or("bad EIP-155 chain id")?;
            let htlc = crate::from_hex_array::<20>(&lock.contract).ok_or("bad HTLC address")?;
            let mut intents = Vec::new();
            match action {
                Action::Lock => {
                    let gross = gross_debit(leg, ctx.transfer_fee.as_ref())?;
                    let (token, value) = if leg.asset.is_native() {
                        if ctx.transfer_fee.is_some() {
                            return Err("the native coin has no transfer fee".into());
                        }
                        ([0u8; 20], leg.amount)
                    } else {
                        let token = leg.asset.erc20_address().ok_or("unsupported EVM asset")?;
                        intents.push(EvmIntent { chain_id, to: token, value: 0, data: evm::approve_calldata(&htlc, gross) });
                        (token, 0)
                    };
                    let call = LockCall {
                        swap_id: lock.swap_id,
                        receiver: evm_addr(&leg.receiver)?,
                        refund_to: evm_addr(&leg.refund_to)?,
                        token,
                        amount: gross,
                        hashlock: lock.hashlock,
                        timelock: unix_time(lock)?,
                    };
                    intents.push(EvmIntent { chain_id, to: htlc, value, data: call.calldata() });
                }
                Action::Reveal | Action::Claim => {
                    intents.push(EvmIntent { chain_id, to: htlc, value: 0, data: evm::claim_calldata(&lock.swap_id, &preimage()?) })
                }
                Action::Refund => intents.push(EvmIntent { chain_id, to: htlc, value: 0, data: evm::refund_calldata(&lock.swap_id) }),
                Action::Accept => return Err("accept has no transaction".into()),
            }
            if txs.len() != intents.len() {
                return Err(format!("expected {} transaction(s)", intents.len()));
            }
            let signing_hashes = txs
                .iter()
                .zip(&intents)
                .map(|(tx, intent)| evm::check_tx(tx, intent).map_err(|e| e.to_string()))
                .collect::<Result<_, _>>()?;
            Ok(TxBinding::Evm { signing_hashes })
        }
        (Family::Solana, ProposedTx::Solana { message }) => {
            let program = solana::parse_key(&lock.contract).ok_or("bad HTLC program id")?;
            let fee_payer = own.solana_fee_payer.ok_or("no own fee payer")?;
            let token = if leg.asset.is_native() {
                None
            } else {
                let mint = leg.asset.spl_mint().ok_or("unsupported Solana asset")?;
                let token_program = ctx.token_program.ok_or("the token program of the mint is not observed")?;
                if token_program != solana::key(solana::TOKEN_PROGRAM) && token_program != solana::key(solana::TOKEN_2022_PROGRAM) {
                    return Err("the mint is not owned by a token program".into());
                }
                Some(TokenAccounts { mint, token_program })
            };
            let accounts = match action {
                Action::Lock => solana::lock_accounts(&program, &lock.swap_id, &sol_key(&leg.sender)?, token.as_ref()),
                Action::Reveal | Action::Claim => solana::spend_accounts(&program, &lock.swap_id, &fee_payer, &sol_key(&leg.receiver)?, token.as_ref()),
                Action::Refund => solana::spend_accounts(&program, &lock.swap_id, &fee_payer, &sol_key(&leg.refund_to)?, token.as_ref()),
                Action::Accept => return Err("accept has no transaction".into()),
            }
            .ok_or("no escrow or token account address")?;
            let htlc_data = match action {
                Action::Lock => {
                    let gross = gross_debit(leg, ctx.transfer_fee.as_ref())?;
                    let mint = token.map_or([0; 32], |t| t.mint);
                    LockData {
                        swap_id: lock.swap_id,
                        receiver: sol_key(&leg.receiver)?,
                        refund_to: sol_key(&leg.refund_to)?,
                        mint,
                        amount: u64::try_from(gross).map_err(|_| "amount exceeds u64")?,
                        hashlock: lock.hashlock,
                        timelock: i64::try_from(unix_time(lock)?).map_err(|_| "timelock exceeds i64")?,
                    }
                    .encode()
                }
                Action::Reveal | Action::Claim => solana::claim_data(&lock.swap_id, &preimage()?),
                Action::Refund => solana::refund_data(&lock.swap_id),
                Action::Accept => return Err("accept has no transaction".into()),
            };
            let intent = SolanaIntent { htlc_program: program, htlc_data, htlc_accounts: accounts, fee_payer, mode: ctx.solana_mode.clone() };
            let message_hash = solana::check_message(message, &ctx.lookup_tables, &intent).map_err(|e| e.to_string())?;
            Ok(TxBinding::Solana { message_hash })
        }
        _ => Err("transaction of another chain family".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::caip::{AccountId, AssetId, ChainId};
    use crate::solana::tests::encode;
    use crate::types::{HashAlg, Lock};

    const CHAIN: &str = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp";
    const SOL: &str = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/slip44:501";
    const USDC: &str = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/token:EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
    const PAYER: Hash32 = [1; 32];
    const PROGRAM: Hash32 = [2; 32];
    const SENDER: Hash32 = [3; 32];
    const RECEIVER: Hash32 = [4; 32];
    const SWAP: Hash32 = [7; 32];

    fn b58(k: &Hash32) -> String {
        bs58::encode(k).into_string()
    }

    fn leg(asset: &str) -> Leg {
        let acct = |k: &Hash32| AccountId::parse(&format!("{CHAIN}:{}", b58(k))).unwrap();
        Leg {
            chain: ChainId::parse(CHAIN).unwrap(),
            asset: AssetId::parse(asset).unwrap(),
            amount: 1_000,
            sender: acct(&SENDER),
            receiver: acct(&RECEIVER),
            refund_to: acct(&SENDER),
            lock: Lock {
                contract: b58(&PROGRAM),
                hash_alg: HashAlg::Sha256,
                hashlock: [0x11; 32],
                preimage_len: 32,
                timelock: TimelockSpec::Time(1_900_000_000),
                swap_id: SWAP,
                keys: None,
            },
        }
    }

    fn own() -> OwnAccounts {
        OwnAccounts { solana_fee_payer: Some(PAYER), ..Default::default() }
    }

    /// `read_only` static keys at the end are read-only; the fee payer is the only signer.
    fn message(keys: &[Hash32], accounts: Vec<u8>, data: Vec<u8>, read_only: u8) -> ProposedTx {
        let mut m = encode(keys, &[(1, accounts, data)], None);
        m[2] = read_only;
        ProposedTx::Solana { message: m }
    }

    #[test]
    fn solana_refund_pays_refund_to_only() {
        let escrow = solana::escrow_address(&PROGRAM, &SWAP).unwrap();
        let keys = [PAYER, PROGRAM, escrow, SENDER, RECEIVER, solana::key(solana::SYSTEM_PROGRAM)];
        let ctx = BindContext::default();
        let refund = message(&keys, vec![0, 2, 3], solana::refund_data(&SWAP), 1);
        assert!(bind(Action::Refund, &leg(SOL), &refund, &own(), &ctx).is_ok());
        let theft = message(&keys, vec![0, 2, 4], solana::refund_data(&SWAP), 1);
        assert!(bind(Action::Refund, &leg(SOL), &theft, &own(), &ctx).is_err());
    }

    #[test]
    fn solana_token_claim_needs_the_observed_token_program() {
        let token = TokenAccounts { mint: AssetId::parse(USDC).unwrap().spl_mint().unwrap(), token_program: solana::key(solana::TOKEN_PROGRAM) };
        let escrow = solana::escrow_address(&PROGRAM, &SWAP).unwrap();
        let ata = |owner: &Hash32| solana::associated_token_address(owner, &token).unwrap();
        let keys = [PAYER, PROGRAM, escrow, ata(&RECEIVER), ata(&escrow), token.mint, token.token_program];
        let preimage = [0x22; 32];
        let claim = message(&keys, vec![0, 2, 3, 4, 5, 6], solana::claim_data(&SWAP, &preimage), 2);
        let mut ctx = BindContext { preimage: Some(preimage), ..Default::default() };
        assert!(bind(Action::Claim, &leg(USDC), &claim, &own(), &ctx).is_err(), "token program not observed");
        ctx.token_program = Some(token.token_program);
        assert!(bind(Action::Claim, &leg(USDC), &claim, &own(), &ctx).is_ok());
        ctx.token_program = Some(solana::key(solana::TOKEN_2022_PROGRAM));
        assert!(bind(Action::Claim, &leg(USDC), &claim, &own(), &ctx).is_err(), "accounts of another token program");
        ctx.token_program = Some([0x0e; 32]);
        assert!(bind(Action::Claim, &leg(USDC), &claim, &own(), &ctx).is_err(), "not a token program");
    }
}
