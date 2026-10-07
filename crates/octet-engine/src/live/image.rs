//! Images attached to a prompt: checked when attached, read when sent.
use std::path::{Path, PathBuf};
use thiserror::Error;

/// The largest image a prompt may carry, in bytes (5 MiB).
pub const IMAGE_LIMIT: u64 = 5 * 1024 * 1024;
/// The most images one prompt may carry.
pub const IMAGES_PER_PROMPT: usize = 4;

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
        })
    }

    /// Reads the file as base64, checking the limit again: it may have
    /// changed since it was attached.
    pub(crate) async fn read_base64(&self) -> Result<String, ImageError> {
        let read = |source| ImageError::Read {
            name: self.name.clone(),
            source,
        };
        let metadata = tokio::fs::metadata(&self.path).await.map_err(read)?;
        if metadata.len() > IMAGE_LIMIT {
            return Err(ImageError::TooLarge(self.name.clone()));
        }
        let bytes = tokio::fs::read(&self.path).await.map_err(read)?;
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
