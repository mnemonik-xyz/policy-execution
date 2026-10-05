//! S24: the agent proposes a transaction; the signer derives the intent from the
//! terms and accepts the transaction only if it does the authorized action and
//! nothing more (spec 4.2). The result is the warrant's transaction binding.

use crate::bitcoin::{self, Leaf};
use crate::caip::Family;
use crate::evm::{self, EvmIntent, LockCall};
use crate::facts::TransferFee;
use crate::solana::{self, LockData, SolanaIntent, SolanaMode};
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
    /// A legacy or version 0 message; lookup addresses as read from the chain.
    Solana { message: Vec<u8>, resolved_lookups: Option<Vec<Hash32>> },
}

/// The signer's own accounts.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct OwnAccounts {
    /// CAIP-10 accounts that receive claims and refunds (S5, S6).
    pub accounts: Vec<crate::caip::AccountId>,
    /// Own Taproot `scriptPubKey`s: inputs and change on Bitcoin.
    pub bitcoin_scripts: Vec<Vec<u8>>,
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
            let binding = match action {
                Action::Lock => {
                    let amount = u64::try_from(leg.amount).map_err(|_| "amount exceeds the Bitcoin range")?;
                    bitcoin::check_lock_psbt(&psbt, lock, amount, &own.bitcoin_scripts)
                }
                Action::Reveal | Action::Claim | Action::Refund => {
                    let (txid, vout) = ctx.htlc_outpoint.ok_or("the HTLC output is not observed")?;
                    let leaf = if action == Action::Refund { Leaf::Refund } else { Leaf::Claim };
                    bitcoin::check_spend_psbt(&psbt, lock, (&txid, vout), leaf, &own.bitcoin_scripts)
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
        (Family::Solana, ProposedTx::Solana { message, resolved_lookups }) => {
            let program = solana::parse_key(&lock.contract).ok_or("bad HTLC program id")?;
            let htlc_data = match action {
                Action::Lock => {
                    let gross = gross_debit(leg, ctx.transfer_fee.as_ref())?;
                    let mint = if leg.asset.is_native() { [0; 32] } else { leg.asset.spl_mint().ok_or("unsupported Solana asset")? };
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
            let intent = SolanaIntent {
                htlc_program: program,
                htlc_data,
                fee_payer: own.solana_fee_payer.ok_or("no own fee payer")?,
                mode: ctx.solana_mode.clone(),
            };
            let message_hash = solana::check_message(message, resolved_lookups.as_deref(), &intent).map_err(|e| e.to_string())?;
            Ok(TxBinding::Solana { message_hash })
        }
        _ => Err("transaction of another chain family".into()),
    }
}
