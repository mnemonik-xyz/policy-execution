//! RFC 8785 JSON Canonicalization Scheme for the values that warrants use.
//!
//! Warrant payloads contain strings, booleans, null, arrays, objects and integers.
//! Integers must lie in the I-JSON safe range (|n| ≤ 2^53 − 1); amounts travel as
//! decimal strings. Floating-point numbers are rejected rather than formatted.

use serde_json::Value;
use std::fmt;

const MAX_SAFE: u64 = (1 << 53) - 1;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum JcsError {
    Float,
    UnsafeInteger,
    Serialize(String),
}

impl fmt::Display for JcsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            JcsError::Float => f.write_str("floating-point numbers are not allowed in a warrant payload"),
            JcsError::UnsafeInteger => f.write_str("integer outside the I-JSON safe range"),
            JcsError::Serialize(e) => write!(f, "serialization failed: {e}"),
        }
    }
}

impl std::error::Error for JcsError {}

/// Canonical JSON text of a value.
pub fn canonicalize(value: &Value) -> Result<String, JcsError> {
    let mut out = String::new();
    write(value, &mut out)?;
    Ok(out)
}

/// Canonical JSON bytes of any serializable value.
pub fn to_vec<T: serde::Serialize>(value: &T) -> Result<Vec<u8>, JcsError> {
    let value = serde_json::to_value(value).map_err(|e| JcsError::Serialize(e.to_string()))?;
    Ok(canonicalize(&value)?.into_bytes())
}

fn write(value: &Value, out: &mut String) -> Result<(), JcsError> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(u) = n.as_u64() {
                if u > MAX_SAFE {
                    return Err(JcsError::UnsafeInteger);
                }
                out.push_str(&u.to_string());
            } else if let Some(i) = n.as_i64() {
                if i.unsigned_abs() > MAX_SAFE {
                    return Err(JcsError::UnsafeInteger);
                }
                out.push_str(&i.to_string());
            } else {
                return Err(JcsError::Float);
            }
        }
        // serde_json escapes exactly as RFC 8785 requires: \" \\ \b \f \n \r \t,
        // other control characters as lowercase \u00xx, everything else literal.
        Value::String(s) => out.push_str(&serde_json::to_string(s).expect("string serialization")),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write(item, out)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            // Keys sort by their UTF-16 code units (RFC 8785 section 3.2.3).
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort_by(|a, b| a.encode_utf16().cmp(b.encode_utf16()));
            out.push('{');
            for (i, key) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&serde_json::to_string(key).expect("string serialization"));
                out.push(':');
                write(&map[*key], out)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn sorts_and_compacts() {
        let v = json!({"b": [1, true, null], "a": {"y": "x", "x": -3}, "": 0});
        assert_eq!(canonicalize(&v).unwrap(), r#"{"":0,"a":{"x":-3,"y":"x"},"b":[1,true,null]}"#);
    }

    #[test]
    fn utf16_key_order() {
        // U+1F600 is the surrogate pair D83D DE00 and sorts before U+FB33 in UTF-16;
        // a UTF-8 byte order would put it after.
        let v = json!({"\u{fb33}": 1, "\u{1f600}": 2, "\u{20ac}": 3, "\r": 4, "1": 5, "\u{80}": 6, "\u{c3}": 7});
        assert_eq!(
            canonicalize(&v).unwrap(),
            "{\"\\r\":4,\"1\":5,\"\u{80}\":6,\"\u{c3}\":7,\"\u{20ac}\":3,\"\u{1f600}\":2,\"\u{fb33}\":1}"
        );
    }

    #[test]
    fn string_escapes() {
        let v = json!("\u{8}\u{c}\n\r\t\"\\\u{1}\u{7f}/é");
        assert_eq!(canonicalize(&v).unwrap(), "\"\\b\\f\\n\\r\\t\\\"\\\\\\u0001\u{7f}/é\"");
    }

    #[test]
    fn rejects_floats_and_unsafe_integers() {
        assert_eq!(canonicalize(&json!(1.5)), Err(JcsError::Float));
        assert_eq!(canonicalize(&json!(1u64 << 53)), Err(JcsError::UnsafeInteger));
        assert_eq!(canonicalize(&json!((1u64 << 53) - 1)).unwrap(), "9007199254740991");
        assert_eq!(canonicalize(&json!(-((1i64 << 53) - 1))).unwrap(), "-9007199254740991");
    }
}
