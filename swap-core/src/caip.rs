//! CAIP-2 chain ids, CAIP-10 accounts and CAIP-19 assets (https://chainagnostic.org).
//!
//! Identifiers are validated on construction. EVM hex addresses are lowercased,
//! so that two spellings of one address compare equal. The verified evaluator
//! sees each identifier only as `sha256(canonical string)`.

use serde::{Deserialize, Serialize};
use std::fmt;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Family {
    Bitcoin,
    Evm,
    Solana,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CaipError(pub String);

impl fmt::Display for CaipError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "invalid CAIP identifier: {}", self.0)
    }
}

impl std::error::Error for CaipError {}

fn charset(text: &str, min: usize, max: usize, allowed: impl Fn(char) -> bool) -> bool {
    (min..=max).contains(&text.len()) && text.chars().all(allowed)
}

fn namespace_ok(ns: &str) -> bool {
    charset(ns, 3, 8, |c| c == '-' || c.is_ascii_lowercase() || c.is_ascii_digit())
}

fn chain_reference_ok(r: &str) -> bool {
    charset(r, 1, 32, |c| c == '-' || c == '_' || c.is_ascii_alphanumeric())
}

fn account_address_ok(a: &str) -> bool {
    charset(a, 1, 128, |c| c == '-' || c == '.' || c == '%' || c.is_ascii_alphanumeric())
}

fn is_evm_address(text: &str) -> bool {
    text.len() == 42 && text.starts_with("0x") && text[2..].chars().all(|c| c.is_ascii_hexdigit())
}

macro_rules! string_id {
    ($name:ident) => {
        impl $name {
            pub fn as_str(&self) -> &str {
                &self.0
            }
            /// The 32-byte identifier that the verified evaluator compares.
            pub fn id(&self) -> crate::Hash32 {
                crate::sha256(self.0.as_bytes())
            }
        }
        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(&self.0)
            }
        }
        impl Serialize for $name {
            fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
                s.serialize_str(&self.0)
            }
        }
        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
                let text = String::deserialize(d)?;
                $name::parse(&text).map_err(serde::de::Error::custom)
            }
        }
    };
}

/// CAIP-2: `namespace:reference`, for example `eip155:1`.
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ChainId(String);
string_id!(ChainId);

impl ChainId {
    pub fn parse(text: &str) -> Result<Self, CaipError> {
        let (ns, reference) = text.split_once(':').ok_or_else(|| CaipError(text.into()))?;
        if !namespace_ok(ns) || !chain_reference_ok(reference) {
            return Err(CaipError(text.into()));
        }
        Ok(ChainId(text.into()))
    }

    pub fn namespace(&self) -> &str {
        self.0.split_once(':').map(|(ns, _)| ns).unwrap_or("")
    }

    pub fn reference(&self) -> &str {
        self.0.split_once(':').map(|(_, r)| r).unwrap_or("")
    }

    /// `None` for a namespace without a reference profile.
    pub fn family(&self) -> Option<Family> {
        match self.namespace() {
            "bip122" => Some(Family::Bitcoin),
            "eip155" => Some(Family::Evm),
            "solana" => Some(Family::Solana),
            _ => None,
        }
    }

    /// The EIP-155 chain id of an `eip155` chain.
    pub fn evm_chain_id(&self) -> Option<u64> {
        if self.namespace() != "eip155" {
            return None;
        }
        self.reference().parse().ok()
    }
}

/// CAIP-10: `chain_id:address`.
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct AccountId(String);
string_id!(AccountId);

impl AccountId {
    pub fn parse(text: &str) -> Result<Self, CaipError> {
        let (chain, address) = text.rsplit_once(':').ok_or_else(|| CaipError(text.into()))?;
        let chain = ChainId::parse(chain)?;
        if !account_address_ok(address) {
            return Err(CaipError(text.into()));
        }
        let address = if chain.family() == Some(Family::Evm) {
            if !is_evm_address(address) {
                return Err(CaipError(text.into()));
            }
            address.to_ascii_lowercase()
        } else {
            address.to_string()
        };
        Ok(AccountId(format!("{chain}:{address}")))
    }

    pub fn chain(&self) -> ChainId {
        ChainId(self.0.rsplit_once(':').map(|(c, _)| c).unwrap_or("").into())
    }

    pub fn address(&self) -> &str {
        self.0.rsplit_once(':').map(|(_, a)| a).unwrap_or("")
    }

    /// The 20-byte address of an EVM account.
    pub fn evm_address(&self) -> Option<[u8; 20]> {
        crate::from_hex_array(self.address())
    }

    /// The 32-byte public key of a Solana account.
    pub fn solana_key(&self) -> Option<[u8; 32]> {
        bs58::decode(self.address()).into_vec().ok()?.try_into().ok()
    }
}

/// CAIP-19: `chain_id/asset_namespace:asset_reference[/token_id]`.
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct AssetId(String);
string_id!(AssetId);

impl AssetId {
    pub fn parse(text: &str) -> Result<Self, CaipError> {
        let mut parts = text.splitn(3, '/');
        let chain = ChainId::parse(parts.next().unwrap_or(""))?;
        let asset = parts.next().ok_or_else(|| CaipError(text.into()))?;
        let token = parts.next();
        let (ns, reference) = asset.split_once(':').ok_or_else(|| CaipError(text.into()))?;
        if !namespace_ok(ns) || !account_address_ok(reference) {
            return Err(CaipError(text.into()));
        }
        if let Some(token) = token {
            if !account_address_ok(token) {
                return Err(CaipError(text.into()));
            }
        }
        let reference = if chain.family() == Some(Family::Evm) && ns == "erc20" {
            if !is_evm_address(reference) {
                return Err(CaipError(text.into()));
            }
            reference.to_ascii_lowercase()
        } else {
            reference.to_string()
        };
        let mut canonical = format!("{chain}/{ns}:{reference}");
        if let Some(token) = token {
            canonical.push('/');
            canonical.push_str(token);
        }
        Ok(AssetId(canonical))
    }

    pub fn chain(&self) -> ChainId {
        ChainId(self.0.split('/').next().unwrap_or("").into())
    }

    pub fn namespace(&self) -> &str {
        self.0.split('/').nth(1).and_then(|a| a.split_once(':')).map(|(ns, _)| ns).unwrap_or("")
    }

    pub fn reference(&self) -> &str {
        self.0.split('/').nth(1).and_then(|a| a.split_once(':')).map(|(_, r)| r).unwrap_or("")
    }

    /// The native coin of the chain (SLIP-44 namespace).
    pub fn is_native(&self) -> bool {
        self.namespace() == "slip44"
    }

    /// The token contract of an ERC-20 asset.
    pub fn erc20_address(&self) -> Option<[u8; 20]> {
        if self.namespace() != "erc20" {
            return None;
        }
        crate::from_hex_array(self.reference())
    }

    /// The mint of an SPL token asset (namespace `token`).
    pub fn spl_mint(&self) -> Option<[u8; 32]> {
        if self.namespace() != "token" {
            return None;
        }
        bs58::decode(self.reference()).into_vec().ok()?.try_into().ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chain_ids() {
        let btc = ChainId::parse("bip122:000000000019d6689c085ae165831e93").unwrap();
        assert_eq!(btc.family(), Some(Family::Bitcoin));
        assert_eq!(ChainId::parse("eip155:1").unwrap().evm_chain_id(), Some(1));
        assert_eq!(ChainId::parse("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp").unwrap().family(), Some(Family::Solana));
        assert_eq!(ChainId::parse("cosmos:cosmoshub-4").unwrap().family(), None);
        for bad in ["eip155", "EIP155:1", "ei:1", "eip155:", "eip155:1:2", "eip155:a/b"] {
            assert!(ChainId::parse(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn accounts_and_assets() {
        let a = AccountId::parse("eip155:1:0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B").unwrap();
        assert_eq!(a.as_str(), "eip155:1:0xab5801a7d398351b8be11c439e05c5b3259aec9b");
        assert_eq!(a.chain().as_str(), "eip155:1");
        assert_eq!(a.evm_address().unwrap()[0], 0xab);
        assert!(AccountId::parse("eip155:1:0x1234").is_err());
        let usdc = AssetId::parse("eip155:1/erc20:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48").unwrap();
        assert_eq!(usdc.as_str(), "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48");
        assert_eq!(usdc.chain().as_str(), "eip155:1");
        assert!(usdc.erc20_address().is_some());
        let btc = AssetId::parse("bip122:000000000019d6689c085ae165831e93/slip44:0").unwrap();
        assert!(btc.is_native());
        let nft = AssetId::parse("eip155:1/erc721:0x06012c8cf97BEaD5deAe237070F9587f8E7A266d/771769").unwrap();
        assert!(nft.as_str().ends_with("/771769"));
        assert!(AssetId::parse("eip155:1").is_err());
        assert!(AssetId::parse("eip155:1/erc20").is_err());
        assert_eq!(usdc.id(), crate::sha256(usdc.as_str().as_bytes()));
    }

    #[test]
    fn serde_validates() {
        assert!(serde_json::from_str::<ChainId>("\"eip155:1\"").is_ok());
        assert!(serde_json::from_str::<ChainId>("\"nope\"").is_err());
    }
}
