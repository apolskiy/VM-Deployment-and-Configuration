// Package main implements the encrypted cluster inventory service.
//
// This file holds the AES-256-GCM layer. The wire format is deliberately
// identical to the one the Python client writes:
//
//	base64( nonce[12] || ciphertext || tag[16] )
//
// which is exactly what gcm.Seal(nonce, nonce, plaintext, nil) produces once
// the result is base64 encoded, so neither side needs a framing header.
package main

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
)

const (
	// keyBytes is the AES-256 key length.
	keyBytes = 32
	// nonceBytes is the GCM standard nonce length.
	nonceBytes = 12
	// tagBytes is the GCM authentication tag length.
	tagBytes = 16
)

// ErrDecryptionFailed reports that ciphertext could not be authenticated.
// Callers map this to HTTP 500 rather than 400, because a tag mismatch means
// the server's own datastore or key is wrong, not that the request was bad.
var ErrDecryptionFailed = errors.New("inventory payload failed AES-256-GCM authentication")

// LoadKey reads a base64-encoded AES-256 key from disk.
//
// The file must decode to exactly 32 bytes. Surrounding whitespace is
// tolerated because editors routinely append a trailing newline.
func LoadKey(path string) ([]byte, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("cannot read inventory key at %s: %w", path, err)
	}

	key, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(raw)))
	if err != nil {
		return nil, fmt.Errorf("inventory key at %s is not valid base64: %w", path, err)
	}
	if len(key) != keyBytes {
		return nil, fmt.Errorf(
			"inventory key at %s decodes to %d bytes, expected %d", path, len(key), keyBytes)
	}
	return key, nil
}

// newGCM builds an AES-256-GCM AEAD from a raw key.
func newGCM(key []byte) (cipher.AEAD, error) {
	if len(key) != keyBytes {
		return nil, fmt.Errorf("AES-256 key must be %d bytes, got %d", keyBytes, len(key))
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("cannot initialise AES cipher: %w", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("cannot initialise GCM mode: %w", err)
	}
	return gcm, nil
}

// Encrypt seals plaintext and returns the base64 wire format.
func Encrypt(plaintext, key []byte) ([]byte, error) {
	gcm, err := newGCM(key)
	if err != nil {
		return nil, err
	}

	nonce := make([]byte, nonceBytes)
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return nil, fmt.Errorf("cannot generate GCM nonce: %w", err)
	}

	sealed := gcm.Seal(nonce, nonce, plaintext, nil)
	encoded := make([]byte, base64.StdEncoding.EncodedLen(len(sealed)))
	base64.StdEncoding.Encode(encoded, sealed)
	return encoded, nil
}

// Decrypt opens a base64 wire-format payload and returns the plaintext.
//
// A tag mismatch is wrapped in ErrDecryptionFailed so callers can distinguish
// tampering or a wrong key from a malformed-encoding problem.
func Decrypt(blob, key []byte) ([]byte, error) {
	gcm, err := newGCM(key)
	if err != nil {
		return nil, err
	}

	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(blob)))
	if err != nil {
		return nil, fmt.Errorf("inventory payload is not valid base64: %w", err)
	}
	if len(raw) < nonceBytes+tagBytes {
		return nil, fmt.Errorf(
			"inventory payload is %d bytes, too short to contain a %d-byte nonce and %d-byte tag",
			len(raw), nonceBytes, tagBytes)
	}

	nonce, sealed := raw[:nonceBytes], raw[nonceBytes:]
	plaintext, err := gcm.Open(nil, nonce, sealed, nil)
	if err != nil {
		return nil, fmt.Errorf("%w: the key is wrong or the ciphertext was altered", ErrDecryptionFailed)
	}
	return plaintext, nil
}
