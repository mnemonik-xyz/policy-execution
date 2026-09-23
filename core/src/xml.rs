//! Restricted XML reader for received invoices. It builds a namespace-resolved
//! element tree and rejects every construct the invoice profile does not need:
//! DTDs, entity declarations, processing instructions after the prolog, and
//! undefined entity references. Limits bound memory and recursion.

pub const MAX_DOCUMENT: usize = 256 * 1024;
const MAX_DEPTH: usize = 32;
const MAX_ELEMENTS: usize = 20_000;
const XMLNS: &str = "http://www.w3.org/2000/xmlns/";

#[derive(Debug, PartialEq, Eq)]
pub struct Element {
    pub ns: String,
    pub local: String,
    /// Attributes without a prefix, as (local name, decoded value).
    pub attrs: Vec<(String, String)>,
    pub children: Vec<Element>,
    /// Concatenated decoded character data directly inside this element.
    pub text: String,
}

impl Element {
    pub fn attr(&self, name: &str) -> Option<&str> {
        self.attrs
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.as_str())
    }

    pub fn children<'a>(
        &'a self,
        ns: &'a str,
        local: &'a str,
    ) -> impl Iterator<Item = &'a Element> {
        self.children
            .iter()
            .filter(move |c| c.ns == ns && c.local == local)
    }

    /// At most one matching child; duplicates of a singleton are rejected.
    pub fn child(&self, ns: &str, local: &str) -> Result<Option<&Element>, XmlError> {
        let mut first = None;
        for c in &self.children {
            if c.ns == ns && c.local == local {
                if first.is_some() {
                    return Err(XmlError::Duplicate);
                }
                first = Some(c);
            }
        }
        Ok(first)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum XmlError {
    TooLarge,
    NotUtf8,
    Forbidden,
    Malformed,
    UnknownPrefix,
    UnknownEntity,
    Limits,
    Duplicate,
}

pub fn parse(bytes: &[u8]) -> Result<Element, XmlError> {
    if bytes.len() > MAX_DOCUMENT {
        return Err(XmlError::TooLarge);
    }
    let text = std::str::from_utf8(bytes).map_err(|_| XmlError::NotUtf8)?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let mut p = Parser {
        s: text,
        i: 0,
        elements: 0,
    };
    p.prolog()?;
    let root = p.element(&[], 0)?;
    p.misc()?;
    if p.i != p.s.len() {
        return Err(XmlError::Malformed);
    }
    Ok(root)
}

struct Parser<'a> {
    s: &'a str,
    i: usize,
    elements: usize,
}

type Scope = [(String, String)];

impl<'a> Parser<'a> {
    fn rest(&self) -> &'a str {
        &self.s[self.i..]
    }

    fn skip_ws(&mut self) {
        let r = self.rest();
        self.i += r.len() - r.trim_start_matches([' ', '\t', '\r', '\n']).len();
    }

    fn eat(&mut self, lit: &str) -> bool {
        if self.rest().starts_with(lit) {
            self.i += lit.len();
            true
        } else {
            false
        }
    }

    fn until(&mut self, end: &str) -> Result<&'a str, XmlError> {
        let r = self.rest();
        let n = r.find(end).ok_or(XmlError::Malformed)?;
        self.i += n + end.len();
        Ok(&r[..n])
    }

    fn prolog(&mut self) -> Result<(), XmlError> {
        let r = self.rest();
        if r.starts_with("<?xml") && r[5..].starts_with([' ', '\t', '\r', '\n']) {
            let decl = self.until("?>")?;
            if decl.contains("standalone=\"no\"") || decl.contains("standalone='no'") {
                return Err(XmlError::Forbidden);
            }
        }
        self.misc()
    }

    /// Whitespace and comments only; DTDs and processing instructions are rejected.
    fn misc(&mut self) -> Result<(), XmlError> {
        loop {
            self.skip_ws();
            if self.eat("<!--") {
                self.comment_body()?;
            } else if self.rest().starts_with("<!") || self.rest().starts_with("<?") {
                return Err(XmlError::Forbidden);
            } else {
                return Ok(());
            }
        }
    }

    fn comment_body(&mut self) -> Result<(), XmlError> {
        let body = self.until("-->")?;
        if body.contains("--") {
            return Err(XmlError::Malformed);
        }
        Ok(())
    }

    fn name(&mut self) -> Result<&'a str, XmlError> {
        let r = self.rest();
        let n = r
            .find(|c: char| c.is_whitespace() || matches!(c, '=' | '/' | '>' | '<' | '"' | '\''))
            .unwrap_or(r.len());
        if n == 0 {
            return Err(XmlError::Malformed);
        }
        self.i += n;
        Ok(&r[..n])
    }

    fn element(&mut self, scope: &Scope, depth: usize) -> Result<Element, XmlError> {
        self.elements += 1;
        if depth > MAX_DEPTH || self.elements > MAX_ELEMENTS {
            return Err(XmlError::Limits);
        }
        if !self.eat("<") {
            return Err(XmlError::Malformed);
        }
        let qname = self.name()?;
        let mut raw_attrs: Vec<(&'a str, String)> = Vec::new();
        let empty;
        loop {
            self.skip_ws();
            if self.eat("/>") {
                empty = true;
                break;
            }
            if self.eat(">") {
                empty = false;
                break;
            }
            let key = self.name()?;
            self.skip_ws();
            if !self.eat("=") {
                return Err(XmlError::Malformed);
            }
            self.skip_ws();
            let quote = if self.eat("\"") {
                "\""
            } else if self.eat("'") {
                "'"
            } else {
                return Err(XmlError::Malformed);
            };
            let raw = self.until(quote)?;
            if raw.contains('<') || raw_attrs.iter().any(|(k, _)| *k == key) {
                return Err(XmlError::Malformed);
            }
            raw_attrs.push((key, decode(raw)?));
        }

        let mut local_scope: Vec<(String, String)> = scope.to_vec();
        for (k, v) in &raw_attrs {
            if *k == "xmlns" {
                local_scope.push((String::new(), v.clone()));
            } else if let Some(prefix) = k.strip_prefix("xmlns:") {
                if v.is_empty() || prefix == "xmlns" {
                    return Err(XmlError::Malformed);
                }
                local_scope.push((prefix.to_string(), v.clone()));
            }
        }
        let resolve = |prefix: &str| -> Result<String, XmlError> {
            match prefix {
                "xml" => Ok("http://www.w3.org/XML/1998/namespace".into()),
                "xmlns" => Ok(XMLNS.into()),
                _ => local_scope
                    .iter()
                    .rev()
                    .find(|(p, _)| p == prefix)
                    .map(|(_, uri)| uri.clone())
                    .ok_or(XmlError::UnknownPrefix),
            }
        };
        let (prefix, local) = qname.split_once(':').unwrap_or(("", qname));
        if local.is_empty() || local.contains(':') {
            return Err(XmlError::Malformed);
        }
        let ns = if prefix.is_empty() {
            local_scope
                .iter()
                .rev()
                .find(|(p, _)| p.is_empty())
                .map(|(_, u)| u.clone())
                .unwrap_or_default()
        } else {
            resolve(prefix)?
        };
        let mut attrs = Vec::new();
        for (k, v) in raw_attrs {
            if k == "xmlns" || k.starts_with("xmlns:") {
                continue;
            }
            if let Some((p, _)) = k.split_once(':') {
                resolve(p)?; // Prefixed attributes are ignored but must be bound.
                continue;
            }
            attrs.push((k.to_string(), v));
        }
        let mut element = Element {
            ns,
            local: local.to_string(),
            attrs,
            children: Vec::new(),
            text: String::new(),
        };
        if empty {
            return Ok(element);
        }

        loop {
            let r = self.rest();
            let n = r.find('<').ok_or(XmlError::Malformed)?;
            if n > 0 {
                let chunk = &r[..n];
                if chunk.contains("]]>") {
                    return Err(XmlError::Malformed);
                }
                element.text.push_str(&decode(chunk)?);
                self.i += n;
            }
            if self.eat("</") {
                let close = self.name()?;
                self.skip_ws();
                if close != qname || !self.eat(">") {
                    return Err(XmlError::Malformed);
                }
                return Ok(element);
            } else if self.eat("<!--") {
                self.comment_body()?;
            } else if self.eat("<![CDATA[") {
                let data = self.until("]]>")?;
                element.text.push_str(data);
            } else if self.rest().starts_with("<!") || self.rest().starts_with("<?") {
                return Err(XmlError::Forbidden);
            } else {
                let child = self.element(&local_scope, depth + 1)?;
                element.children.push(child);
            }
        }
    }
}

/// Decodes the five predefined entities and numeric character references only.
fn decode(raw: &str) -> Result<String, XmlError> {
    let mut out = String::with_capacity(raw.len());
    let mut rest = raw;
    while let Some(amp) = rest.find('&') {
        out.push_str(&rest[..amp]);
        let after = &rest[amp + 1..];
        let semi = after.find(';').ok_or(XmlError::UnknownEntity)?;
        let entity = &after[..semi];
        let c = match entity {
            "amp" => '&',
            "lt" => '<',
            "gt" => '>',
            "quot" => '"',
            "apos" => '\'',
            _ => {
                let code = if let Some(hex) = entity.strip_prefix("#x") {
                    u32::from_str_radix(hex, 16)
                } else if let Some(dec) = entity.strip_prefix('#') {
                    dec.parse::<u32>()
                } else {
                    return Err(XmlError::UnknownEntity);
                };
                let c = code
                    .ok()
                    .and_then(char::from_u32)
                    .ok_or(XmlError::UnknownEntity)?;
                if c == '\0' {
                    return Err(XmlError::UnknownEntity);
                }
                c
            }
        };
        out.push(c);
        rest = &after[semi + 1..];
    }
    out.push_str(rest);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolves_rebound_prefixes_and_decodes_entities() {
        let doc = br#"<?xml version="1.0"?><a:R xmlns:a="urn:x" xmlns:b="urn:y"><b:C k="1&amp;2">x &lt; y<![CDATA[<z>]]></b:C><x:C xmlns:x="urn:y"/></a:R>"#;
        let root = parse(doc).unwrap();
        assert_eq!((root.ns.as_str(), root.local.as_str()), ("urn:x", "R"));
        let c: Vec<_> = root.children("urn:y", "C").collect();
        assert_eq!(c.len(), 2);
        assert_eq!(c[0].text, "x < y<z>");
        assert_eq!(c[0].attr("k"), Some("1&2"));
        assert_eq!(root.child("urn:y", "C"), Err(XmlError::Duplicate));
    }

    #[test]
    fn rejects_dtds_entities_and_processing_instructions() {
        for doc in [
            &b"<!DOCTYPE r [<!ENTITY e \"x\">]><r>&e;</r>"[..],
            b"<r>&e;</r>",
            b"<r><?pi x?></r>",
            b"<r><!ENTITY e \"x\"></r>",
            b"<?xml version=\"1.0\"?><?pi?><r/>",
            b"<r>&#0;</r>",
        ] {
            assert!(parse(doc).is_err(), "{}", String::from_utf8_lossy(doc));
        }
    }

    #[test]
    fn rejects_malformed_and_unbound_input() {
        for doc in [
            &b"<r><a></r>"[..],
            b"<r/>trailing",
            b"<p:r/>",
            b"<r a='1' a='2'/>",
            b"\xff",
        ] {
            assert!(parse(doc).is_err());
        }
        let deep = format!("{}{}", "<a>".repeat(40), "</a>".repeat(40));
        assert_eq!(parse(deep.as_bytes()), Err(XmlError::Limits));
    }
}
