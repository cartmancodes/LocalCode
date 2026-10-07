//! Images attached to a prompt: checked when attached, read when sent.
use std::path::{Path, PathBuf};
use thiserror::Error;
use tokio::io::AsyncReadExt;

/// The largest image a prompt may carry, in bytes (5 MiB).
pub const IMAGE_LIMIT: u64 = 5 * 1024 * 1024;
/// The most images one prompt may carry.
pub const IMAGES_PER_PROMPT: usize = 4;
/// The largest base64 image a vendor takes inline (Anthropic's 5 MB).
const INLINE_IMAGE_LIMIT: u64 = 5 * 1024 * 1024;
/// The most base64 image data one prompt carries inline, leaving room in
/// the 8 MiB frame for the text.
const INLINE_TOTAL_LIMIT: u64 = 7 * 1024 * 1024;

/// An image file to send with a prompt. Only its path is kept; the bytes are
/// read when the prompt is sent, and never journaled.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImageAttachment {
    /// The absolute path to the file.
    pub path: PathBuf,
    /// Its MIME type, from the extension: `image/png`, `image/jpeg`,
    /// `image/gif` or `image/webp`.
    pub media_type: &'static str,
    /// The file name, as shown in the prompt box and the transcript.
    pub name: String,
    /// Its size when attached.
    pub bytes: u64,
}

/// Why an image cannot be attached or sent.
#[derive(Debug, Error)]
pub enum ImageError {
    /// The extension is not a supported image format.
    #[error("{0} is not a PNG, JPEG, GIF or WebP image")]
    Format(String),
    /// The file cannot be read.
    #[error("Cannot read {name}: {source}")]
    Read {
        /// The file name.
        name: String,
        /// The I/O failure.
        source: std::io::Error,
    },
    /// The file is a directory or another non-file.
    #[error("{0} is not a file")]
    NotFile(String),
    /// The file is over [`IMAGE_LIMIT`].
    #[error("{0} is over 5 MiB")]
    TooLarge(String),
    /// Too large for a provider that takes images inline.
    #[error("{name} is over 3.75 MiB, the largest image {provider} accepts")]
    InlineTooLarge {
        /// The file name.
        name: String,
        /// The provider's title.
        provider: String,
    },
    /// Together too large for one inline prompt.
    #[error("These images are over 5.25 MiB together, the most {0} accepts in one prompt")]
    InlineTotal(String),
}

/// The base64 length of `raw` bytes.
pub fn encoded_len(raw: u64) -> u64 {
    raw.div_ceil(3) * 4
}

/// Checks images sent inline by `provider`, given as (name, base64 length):
/// each within the vendor's per-image limit, all within one frame.
///
/// # Errors
///
/// Names the first image over 3.75 MiB (5 MiB in base64), or the set when
/// together they are over 5.25 MiB (7 MiB in base64).
pub fn check_inline<'a>(
    provider: &str,
    images: impl IntoIterator<Item = (&'a str, u64)>,
) -> Result<(), ImageError> {
    let mut total = 0;
    for (name, encoded) in images {
        if encoded > INLINE_IMAGE_LIMIT {
            return Err(ImageError::InlineTooLarge {
                name: name.to_owned(),
                provider: provider.to_owned(),
            });
        }
        total += encoded;
    }
    if total > INLINE_TOTAL_LIMIT {
        return Err(ImageError::InlineTotal(provider.to_owned()));
    }
    Ok(())
}

impl ImageAttachment {
    /// Checks `path` (format by extension, a readable file, at most 5 MiB)
    /// and resolves it to an absolute path.
    ///
    /// # Errors
    ///
    /// Fails if the extension is not png, jpg, jpeg, gif or webp, the file
    /// cannot be read, it is not a file, or it is over [`IMAGE_LIMIT`].
    pub fn open(path: &Path) -> Result<Self, ImageError> {
        let name = path.file_name().map_or_else(
            || path.display().to_string(),
            |n| n.to_string_lossy().into_owned(),
        );
        let media_type = path
            .extension()
            .and_then(|e| e.to_str())
            .and_then(media_type)
            .ok_or_else(|| ImageError::Format(name.clone()))?;
        let read = |source| ImageError::Read {
            name: name.clone(),
            source,
        };
        let path = path.canonicalize().map_err(read)?;
        let metadata = std::fs::metadata(&path).map_err(read)?;
        if !metadata.is_file() {
            return Err(ImageError::NotFile(name));
        }
        if metadata.len() > IMAGE_LIMIT {
            return Err(ImageError::TooLarge(name));
        }
        Ok(Self {
            path,
            media_type,
            name,
            bytes: metadata.len(),
        })
    }

    /// Reads the file as base64, checking the limit again: it may have
    /// changed since it was attached.
    pub(crate) async fn read_base64(&self) -> Result<String, ImageError> {
        let read = |source| ImageError::Read {
            name: self.name.clone(),
            source,
        };
        // Read at most one byte past the limit: the file may have grown since
        // it was attached, and the read itself must stay bounded.
        let mut bytes = Vec::new();
        tokio::fs::File::open(&self.path)
            .await
            .map_err(read)?
            .take(IMAGE_LIMIT + 1)
            .read_to_end(&mut bytes)
            .await
            .map_err(read)?;
        if bytes.len() as u64 > IMAGE_LIMIT {
            return Err(ImageError::TooLarge(self.name.clone()));
        }
        Ok(base64(&bytes))
    }
}

/// The MIME type for a supported image extension, ignoring case.
fn media_type(extension: &str) -> Option<&'static str> {
    match extension.to_ascii_lowercase().as_str() {
        "png" => Some("image/png"),
        "jpg" | "jpeg" => Some("image/jpeg"),
        "gif" => Some("image/gif"),
        "webp" => Some("image/webp"),
        _ => None,
    }
}

/// Standard base64 (RFC 4648) with padding.
fn base64(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            chunk.get(1).copied().unwrap_or(0),
            chunk.get(2).copied().unwrap_or(0),
        ];
        let n = u32::from(b[0]) << 16 | u32::from(b[1]) << 8 | u32::from(b[2]);
        for (i, shift) in [18, 12, 6, 0].into_iter().enumerate() {
            if i <= chunk.len() {
                out.push(char::from(ALPHABET[(n >> shift & 63) as usize]));
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
    use octet_testkit::TempDir;

    /// A fresh directory; `TempDir` names it, the test creates it.
    fn folder() -> TempDir {
        let dir = TempDir::new("octet-image");
        std::fs::create_dir_all(dir.path()).unwrap();
        dir
    }

    #[test]
    fn base64_matches_rfc_4648() {
        for (input, encoded) in [
            ("", ""),
            ("f", "Zg=="),
            ("fo", "Zm8="),
            ("foo", "Zm9v"),
            ("foob", "Zm9vYg=="),
            ("fooba", "Zm9vYmE="),
            ("foobar", "Zm9vYmFy"),
        ] {
            assert_eq!(base64(input.as_bytes()), encoded, "{input}");
        }
        assert_eq!(base64(&[0xff, 0xfe, 0xfd]), "//79");
    }

    #[test]
    fn inline_images_fit_the_vendor_and_the_frame() {
        let mib = 1024 * 1024;
        assert_eq!(encoded_len(3), 4);
        assert_eq!(encoded_len(4), 8);
        assert!(check_inline("Claude", [("a.png", encoded_len(3 * mib))]).is_ok());
        let one = check_inline("Claude", [("big.png", encoded_len(4 * mib))]).unwrap_err();
        assert_eq!(
            one.to_string(),
            "big.png is over 3.75 MiB, the largest image Claude accepts"
        );
        let two = [
            ("a.png", encoded_len(3 * mib)),
            ("b.png", encoded_len(3 * mib)),
        ];
        assert_eq!(
            check_inline("Claude", two).unwrap_err().to_string(),
            "These images are over 5.25 MiB together, the most Claude accepts in one prompt"
        );
    }

    #[tokio::test]
    async fn an_image_over_the_limit_is_refused_when_read() {
        let dir = folder();
        let path = dir.path().join("grown.png");
        std::fs::write(&path, b"x").unwrap();
        let image = ImageAttachment::open(&path).unwrap();
        // The file grows after it was attached.
        std::fs::File::create(&path)
            .unwrap()
            .set_len(IMAGE_LIMIT + 1)
            .unwrap();
        assert!(matches!(
            image.read_base64().await,
            Err(ImageError::TooLarge(_))
        ));
    }

    #[test]
    fn open_accepts_supported_images() {
        let dir = folder();
        for (file, media) in [
            ("a.png", "image/png"),
            ("b.JPG", "image/jpeg"),
            ("c.jpeg", "image/jpeg"),
            ("d.gif", "image/gif"),
            ("e.webp", "image/webp"),
        ] {
            let path = dir.path().join(file);
            std::fs::write(&path, b"x").unwrap();
            let image = ImageAttachment::open(&path).unwrap();
            assert_eq!(image.media_type, media);
            assert_eq!(image.name, file);
            assert!(image.path.is_absolute());
        }
    }

    #[test]
    fn open_refuses_other_formats_missing_files_and_large_images() {
        let dir = folder();
        let text = dir.path().join("notes.txt");
        std::fs::write(&text, b"x").unwrap();
        assert!(matches!(
            ImageAttachment::open(&text),
            Err(ImageError::Format(_))
        ));
        assert!(matches!(
            ImageAttachment::open(&dir.path().join("gone.png")),
            Err(ImageError::Read { .. })
        ));
        let folder = dir.path().join("folder.png");
        std::fs::create_dir(&folder).unwrap();
        assert!(matches!(
            ImageAttachment::open(&folder),
            Err(ImageError::NotFile(_))
        ));
        let big = dir.path().join("big.png");
        std::fs::File::create(&big)
            .unwrap()
            .set_len(IMAGE_LIMIT + 1)
            .unwrap();
        let error = ImageAttachment::open(&big).unwrap_err();
        assert_eq!(error.to_string(), "big.png is over 5 MiB");
        let exact = dir.path().join("exact.png");
        std::fs::File::create(&exact)
            .unwrap()
            .set_len(IMAGE_LIMIT)
            .unwrap();
        assert!(ImageAttachment::open(&exact).is_ok());
    }
}
