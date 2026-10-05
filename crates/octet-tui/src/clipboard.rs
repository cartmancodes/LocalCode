//! `/copy`: text to the clipboard through OSC 52, which terminals, tmux
//! (`set -g set-clipboard on`) and mosh 1.4+ pass on, even to a phone.

/// The most text sent in one copy.
pub const LIMIT: usize = 100 * 1024;

/// The escape that sets the clipboard, and whether `text` was cut to fit.
/// None for empty text.
pub fn osc52(text: &str) -> Option<(Vec<u8>, bool)> {
    if text.is_empty() {
        return None;
    }
    let end = text.floor_char_boundary(LIMIT.min(text.len()));
    let mut bytes = b"\x1b]52;c;".to_vec();
    bytes.extend_from_slice(base64(&text.as_bytes()[..end]).as_bytes());
    bytes.push(0x07);
    Some((bytes, end < text.len()))
}

fn base64(data: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let padded = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = u32::from(padded[0]) << 16 | u32::from(padded[1]) << 8 | u32::from(padded[2]);
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(TABLE[(n >> (18 - 6 * i)) as usize & 63] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn encodes_known_strings() {
        assert_eq!(base64(b""), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v");
        assert_eq!(base64(b"foobar"), "Zm9vYmFy");
        assert_eq!(osc52("hi"), Some((b"\x1b]52;c;aGk=\x07".to_vec(), false)));
        assert_eq!(osc52(""), None);
    }
    #[test]
    fn cuts_long_text_on_a_character_boundary() {
        let text = "é".repeat(LIMIT);
        let (bytes, cut) = osc52(&text).unwrap();
        assert!(cut);
        let encoded = &bytes[7..bytes.len() - 1];
        assert!(encoded.len() <= LIMIT.div_ceil(3) * 4);
        assert_eq!(encoded.len() % 4, 0);
    }
}
