//! The policy signer's own state: ledger facts (spend per period, open swaps) and
//! the consumed sets of S4, S10 and S21, the policy version of S22 and the
//! monotonic counter of S25. Persistence belongs to the signer (W3); this module
//! defines the state and its rules.

use crate::Hash32;
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Spend {
    pub time: u64,
    pub notional: u64,
    pub swap_id: Hash32,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Ledger {
    /// Increases with every change; persisted with the state.
    pub counter: u64,
    /// Highest policy version that the signer has accepted (S22).
    pub policy_version: u64,
    pub consumed_swap_ids: BTreeSet<Hash32>,
    pub consumed_hashlocks: BTreeSet<Hash32>,
    pub consumed_warrants: BTreeSet<Hash32>,
    pub open_swaps: BTreeSet<Hash32>,
    pub spends: Vec<Spend>,
}

/// The ledger as loaded, with the last counter that the signer knows it persisted
/// (for example from a monotonic hardware counter or the anchored record chain).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct LedgerState {
    pub ledger: Ledger,
    pub persisted_counter: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LedgerError {
    SwapIdConsumed,
    HashlockConsumed,
    WarrantConsumed,
    PolicyRollback { seen: u64, offered: u64 },
}

impl LedgerState {
    /// S25: a ledger behind its persisted counter lost state; its facts are unknown.
    pub fn trustworthy(&self) -> bool {
        self.ledger.counter >= self.persisted_counter
    }

    /// Spend in the window `(now − period, now]`; `None` when untrustworthy.
    pub fn period_spent(&self, period: u64, now: u64) -> Option<u64> {
        self.period_spent_excluding(period, now, None)
    }

    /// As `period_spent`, without the spend of `swap_id`: the evaluator adds the
    /// notional of the swap it decides, so an accepted swap is not counted twice.
    pub fn period_spent_excluding(&self, period: u64, now: u64, swap_id: Option<&Hash32>) -> Option<u64> {
        if !self.trustworthy() {
            return None;
        }
        let start = now.saturating_sub(period);
        self.ledger
            .spends
            .iter()
            .filter(|s| s.time > start && s.time <= now && Some(&s.swap_id) != swap_id)
            .try_fold(0u64, |acc, s| acc.checked_add(s.notional))
    }

    /// Open swaps including `swap_id`; `None` when untrustworthy.
    pub fn open_swaps_including(&self, swap_id: &Hash32) -> Option<u64> {
        if !self.trustworthy() {
            return None;
        }
        let extra = u64::from(!self.ledger.open_swaps.contains(swap_id));
        Some(self.ledger.open_swaps.len() as u64 + extra)
    }

    /// S22: the policy version only increases.
    pub fn check_policy_version(&self, version: u64) -> Result<(), LedgerError> {
        if version < self.ledger.policy_version {
            return Err(LedgerError::PolicyRollback { seen: self.ledger.policy_version, offered: version });
        }
        Ok(())
    }

    fn bump(&mut self) {
        self.ledger.counter += 1;
        self.persisted_counter = self.ledger.counter;
    }

    pub fn accept_policy_version(&mut self, version: u64) -> Result<(), LedgerError> {
        self.check_policy_version(version)?;
        self.ledger.policy_version = version;
        self.bump();
        Ok(())
    }

    /// Record an accepted swap: consume its swap id and hashlock (S4, S10), open
    /// it and count its notional against every period.
    pub fn record_accept(&mut self, swap_id: Hash32, hashlock: Hash32, notional: u64, now: u64) -> Result<(), LedgerError> {
        if self.ledger.consumed_swap_ids.contains(&swap_id) {
            return Err(LedgerError::SwapIdConsumed);
        }
        if self.ledger.consumed_hashlocks.contains(&hashlock) {
            return Err(LedgerError::HashlockConsumed);
        }
        self.ledger.consumed_swap_ids.insert(swap_id);
        self.ledger.consumed_hashlocks.insert(hashlock);
        self.ledger.open_swaps.insert(swap_id);
        self.ledger.spends.push(Spend { time: now, notional, swap_id });
        self.bump();
        Ok(())
    }

    /// S21: consume a warrant once.
    pub fn consume_warrant(&mut self, warrant_hash: Hash32) -> Result<(), LedgerError> {
        if !self.ledger.consumed_warrants.insert(warrant_hash) {
            return Err(LedgerError::WarrantConsumed);
        }
        self.bump();
        Ok(())
    }

    pub fn close_swap(&mut self, swap_id: &Hash32) {
        if self.ledger.open_swaps.remove(swap_id) {
            self.bump();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spend_windows() {
        let mut s = LedgerState::default();
        s.record_accept([1; 32], [11; 32], 100, 1_000).unwrap();
        s.record_accept([2; 32], [12; 32], 50, 90_000).unwrap();
        assert_eq!(s.period_spent(86_400, 90_000), Some(50));
        assert_eq!(s.period_spent(89_000, 90_000), Some(50), "window is (now - period, now]");
        assert_eq!(s.period_spent(89_001, 90_000), Some(150));
        assert_eq!(s.open_swaps_including(&[3; 32]), Some(3));
        assert_eq!(s.open_swaps_including(&[1; 32]), Some(2));
        s.close_swap(&[1; 32]);
        assert_eq!(s.open_swaps_including(&[3; 32]), Some(2));
    }

    #[test]
    fn replay_and_reuse() {
        let mut s = LedgerState::default();
        s.record_accept([1; 32], [11; 32], 1, 1).unwrap();
        assert_eq!(s.record_accept([1; 32], [12; 32], 1, 1), Err(LedgerError::SwapIdConsumed));
        assert_eq!(s.record_accept([2; 32], [11; 32], 1, 1), Err(LedgerError::HashlockConsumed));
        s.consume_warrant([5; 32]).unwrap();
        assert_eq!(s.consume_warrant([5; 32]), Err(LedgerError::WarrantConsumed));
    }

    #[test]
    fn policy_rollback() {
        let mut s = LedgerState::default();
        s.accept_policy_version(3).unwrap();
        assert_eq!(s.accept_policy_version(2), Err(LedgerError::PolicyRollback { seen: 3, offered: 2 }));
        s.accept_policy_version(3).unwrap();
    }

    #[test]
    fn restart_with_lost_state_gives_unknown() {
        let mut s = LedgerState::default();
        s.record_accept([1; 32], [11; 32], 100, 1_000).unwrap();
        // The signer restarts from an older snapshot: counter behind the persisted counter.
        let restored = LedgerState { ledger: Ledger::default(), persisted_counter: s.persisted_counter };
        assert!(!restored.trustworthy());
        assert_eq!(restored.period_spent(86_400, 1_000), None);
        assert_eq!(restored.open_swaps_including(&[2; 32]), None);
    }
}
