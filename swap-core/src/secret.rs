//! The swap secret (S4). Generated from a CSPRNG that the caller supplies, used
//! for one swap only, never printed, never serialized, zeroized on drop. Only the
//! reveal path consumes it.

use rand_core::{CryptoRng, RngCore};
use zeroize::Zeroize;

pub struct Secret([u8; 32]);

impl Secret {
    pub fn generate<R: RngCore + CryptoRng>(rng: &mut R) -> Self {
        let mut bytes = [0u8; 32];
        rng.fill_bytes(&mut bytes);
        Secret(bytes)
    }

    /// For the signer's own encrypted store only; never from the agent.
    pub fn from_store(bytes: [u8; 32]) -> Self {
        Secret(bytes)
    }

    /// `H = sha256(s)`.
    pub fn hashlock(&self) -> crate::Hash32 {
        crate::sha256(&self.0)
    }

    /// Hand the preimage to the claim transaction builder of the reveal action.
    pub fn reveal(self) -> [u8; 32] {
        self.0
    }
}

impl Drop for Secret {
    fn drop(&mut self) {
        self.0.zeroize();
    }
}

/// A preimage must be exactly 32 bytes and hash to the lock's `H` (S1, S2).
pub fn preimage_opens(preimage: &[u8], hashlock: &crate::Hash32) -> bool {
    preimage.len() == 32 && &crate::sha256(preimage) == hashlock
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hashlock_and_preimage() {
        let s = Secret::generate(&mut rand_core::OsRng);
        let h = s.hashlock();
        let p = s.reveal();
        assert!(preimage_opens(&p, &h));
        // Fault test 1: a 33-byte preimage never opens a lock.
        let mut long = p.to_vec();
        long.push(0);
        assert!(!preimage_opens(&long, &crate::sha256(&long)));
    }

    #[test]
    fn distinct_secrets() {
        let a = Secret::generate(&mut rand_core::OsRng);
        let b = Secret::generate(&mut rand_core::OsRng);
        assert_ne!(a.hashlock(), b.hashlock());
    }
}
