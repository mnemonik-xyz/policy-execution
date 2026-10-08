//! Warrant for swaps: the deterministic core of the policy signer (step W2).
//!
//! The agent proposes; this crate decides what may be signed. It builds facts with
//! provenance, runs the obligatory safety checks S1 to S25 and S27, calls the verified
//! evaluator of `warrant-swap-verified` and produces unsigned warrant payloads.
//! It reads no network, no clock and no key. Time and chain state are inputs.

pub mod authorize;
pub mod bitcoin;
pub mod caip;
pub mod checks;
pub mod dsl;
pub mod evm;
pub mod facts;
pub mod jcs;
pub mod ledger;
pub mod profile;
pub mod secret;
pub mod solana;
pub mod tx;
pub mod types;
pub mod warrant;

pub use warrant_swap_verified as verified;

pub type Hash32 = [u8; 32];

pub fn sha256(data: &[u8]) -> Hash32 {
    use sha2::{Digest, Sha256};
    Sha256::digest(data).into()
}

pub fn blake3(data: &[u8]) -> Hash32 {
    *::blake3::hash(data).as_bytes()
}

pub fn keccak256(data: &[u8]) -> Hash32 {
    use sha3::{Digest, Keccak256};
    Keccak256::digest(data).into()
}

/// Lowercase hexadecimal without a prefix.
pub fn to_hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(DIGITS[(b >> 4) as usize] as char);
        out.push(DIGITS[(b & 15) as usize] as char);
    }
    out
}

/// Accepts lowercase or uppercase hexadecimal, with an optional `0x` prefix.
pub fn from_hex(text: &str) -> Option<Vec<u8>> {
    let text = text.strip_prefix("0x").unwrap_or(text);
    if !text.len().is_multiple_of(2) {
        return None;
    }
    let digit = |c: u8| match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    };
    text.as_bytes()
        .chunks(2)
        .map(|pair| Some(digit(pair[0])? << 4 | digit(pair[1])?))
        .collect()
}

pub fn from_hex_array<const N: usize>(text: &str) -> Option<[u8; N]> {
    from_hex(text)?.try_into().ok()
}

/// Serde encodings for warrant payloads: bytes as lowercase hex, amounts as
/// decimal strings (JCS numbers are doubles and lose precision above 2^53).
pub mod enc {
    use serde::{de::Error, Deserialize, Deserializer, Serializer};

    pub mod hex32 {
        use super::*;
        pub fn serialize<S: Serializer>(v: &[u8; 32], s: S) -> Result<S::Ok, S::Error> {
            s.serialize_str(&crate::to_hex(v))
        }
        pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<[u8; 32], D::Error> {
            let text = String::deserialize(d)?;
            crate::from_hex_array(&text).ok_or_else(|| D::Error::custom("expected 32 bytes of hex"))
        }
    }

    pub mod hex16 {
        use super::*;
        pub fn serialize<S: Serializer>(v: &[u8; 16], s: S) -> Result<S::Ok, S::Error> {
            s.serialize_str(&crate::to_hex(v))
        }
        pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<[u8; 16], D::Error> {
            let text = String::deserialize(d)?;
            crate::from_hex_array(&text).ok_or_else(|| D::Error::custom("expected 16 bytes of hex"))
        }
    }

    pub mod hex32_opt {
        use super::*;
        pub fn serialize<S: Serializer>(v: &Option<[u8; 32]>, s: S) -> Result<S::Ok, S::Error> {
            match v {
                Some(v) => s.serialize_str(&crate::to_hex(v)),
                None => s.serialize_none(),
            }
        }
        pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Option<[u8; 32]>, D::Error> {
            match Option::<String>::deserialize(d)? {
                Some(text) => crate::from_hex_array(&text)
                    .map(Some)
                    .ok_or_else(|| D::Error::custom("expected 32 bytes of hex")),
                None => Ok(None),
            }
        }
    }

    pub mod hex20_opt {
        use super::*;
        pub fn serialize<S: Serializer>(v: &Option<[u8; 20]>, s: S) -> Result<S::Ok, S::Error> {
            match v {
                Some(v) => s.serialize_str(&crate::to_hex(v)),
                None => s.serialize_none(),
            }
        }
        pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Option<[u8; 20]>, D::Error> {
            match Option::<String>::deserialize(d)? {
                Some(text) => crate::from_hex_array(&text)
                    .map(Some)
                    .ok_or_else(|| D::Error::custom("expected 20 bytes of hex")),
                None => Ok(None),
            }
        }
    }

    pub mod hex32_vec {
        use super::*;
        use serde::ser::SerializeSeq;
        pub fn serialize<S: Serializer>(v: &[[u8; 32]], s: S) -> Result<S::Ok, S::Error> {
            let mut seq = s.serialize_seq(Some(v.len()))?;
            for item in v {
                seq.serialize_element(&crate::to_hex(item))?;
            }
            seq.end()
        }
        pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<[u8; 32]>, D::Error> {
            Vec::<String>::deserialize(d)?
                .iter()
                .map(|t| crate::from_hex_array(t).ok_or_else(|| D::Error::custom("expected 32 bytes of hex")))
                .collect()
        }
    }

    pub mod amount {
        use super::*;
        pub fn serialize<S: Serializer>(v: &u128, s: S) -> Result<S::Ok, S::Error> {
            s.serialize_str(&v.to_string())
        }
        pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<u128, D::Error> {
            let text = String::deserialize(d)?;
            if text.is_empty() || !text.bytes().all(|c| c.is_ascii_digit()) || (text.len() > 1 && text.starts_with('0')) {
                return Err(D::Error::custom("expected an integer amount as a decimal string"));
            }
            text.parse().map_err(D::Error::custom)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_round_trip() {
        assert_eq!(to_hex(&[0, 1, 0xab, 0xff]), "0001abff");
        assert_eq!(from_hex("0x0001ABff").unwrap(), vec![0, 1, 0xab, 0xff]);
        assert!(from_hex("abc").is_none());
        assert!(from_hex("zz").is_none());
    }

    #[test]
    fn hash_vectors() {
        assert_eq!(to_hex(&sha256(b"abc")), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
        assert_eq!(to_hex(&keccak256(b"")), "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470");
        assert_eq!(to_hex(&blake3(b"")), "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262");
    }
}
