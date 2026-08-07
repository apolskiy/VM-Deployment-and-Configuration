package main

import (
	"encoding/base64"
	"errors"
	"strings"
	"testing"
)

// testKey returns a deterministic 32-byte key for tests.
func testKey() []byte {
	key := make([]byte, keyBytes)
	for index := range key {
		key[index] = byte(index)
	}
	return key
}

func TestEncryptDecryptRoundTrip(t *testing.T) {
	key := testKey()
	plaintext := []byte(`[{"hostname":"apnode1","role":"backend"}]`)

	sealed, err := Encrypt(plaintext, key)
	if err != nil {
		t.Fatalf("Encrypt returned an error: %v", err)
	}
	if strings.Contains(string(sealed), "apnode1") {
		t.Fatal("ciphertext leaks plaintext content")
	}

	opened, err := Decrypt(sealed, key)
	if err != nil {
		t.Fatalf("Decrypt returned an error: %v", err)
	}
	if string(opened) != string(plaintext) {
		t.Fatalf("round trip mismatch: got %q, want %q", opened, plaintext)
	}
}

func TestEncryptUsesFreshNonce(t *testing.T) {
	key := testKey()
	plaintext := []byte("identical plaintext")

	first, err := Encrypt(plaintext, key)
	if err != nil {
		t.Fatalf("first Encrypt failed: %v", err)
	}
	second, err := Encrypt(plaintext, key)
	if err != nil {
		t.Fatalf("second Encrypt failed: %v", err)
	}
	if string(first) == string(second) {
		t.Fatal("encrypting the same plaintext twice produced identical ciphertext; nonce is not fresh")
	}
}

func TestDecryptRejectsWrongKey(t *testing.T) {
	sealed, err := Encrypt([]byte("secret"), testKey())
	if err != nil {
		t.Fatalf("Encrypt failed: %v", err)
	}

	wrong := make([]byte, keyBytes)
	for index := range wrong {
		wrong[index] = 0xAA
	}

	if _, err := Decrypt(sealed, wrong); !errors.Is(err, ErrDecryptionFailed) {
		t.Fatalf("expected ErrDecryptionFailed for a wrong key, got %v", err)
	}
}

func TestDecryptRejectsTamperedCiphertext(t *testing.T) {
	key := testKey()
	sealed, err := Encrypt([]byte("secret payload"), key)
	if err != nil {
		t.Fatalf("Encrypt failed: %v", err)
	}

	raw, err := base64.StdEncoding.DecodeString(string(sealed))
	if err != nil {
		t.Fatalf("could not decode ciphertext: %v", err)
	}
	// Flip a bit inside the ciphertext body, past the nonce.
	raw[nonceBytes+1] ^= 0x01
	tampered := []byte(base64.StdEncoding.EncodeToString(raw))

	if _, err := Decrypt(tampered, key); !errors.Is(err, ErrDecryptionFailed) {
		t.Fatalf("expected ErrDecryptionFailed for tampered ciphertext, got %v", err)
	}
}

func TestDecryptRejectsShortPayload(t *testing.T) {
	short := []byte(base64.StdEncoding.EncodeToString([]byte("tooshort")))
	if _, err := Decrypt(short, testKey()); err == nil {
		t.Fatal("expected an error for a payload shorter than nonce plus tag")
	}
}

func TestDecryptRejectsInvalidBase64(t *testing.T) {
	if _, err := Decrypt([]byte("not base64 !!!"), testKey()); err == nil {
		t.Fatal("expected an error for a non-base64 payload")
	}
}

func TestNewGCMRejectsWrongKeyLength(t *testing.T) {
	if _, err := newGCM(make([]byte, 16)); err == nil {
		t.Fatal("expected an error for a 16-byte key")
	}
}

func TestRecordValidate(t *testing.T) {
	cases := []struct {
		name    string
		record  Record
		wantErr bool
	}{
		{"valid jump", Record{Hostname: "apjump", Role: "jump"}, false},
		{"valid backend", Record{Hostname: "apnode1", Role: "backend"}, false},
		{"missing hostname", Record{Role: "backend"}, true},
		{"bad role", Record{Hostname: "apnode1", Role: "database"}, true},
	}

	for _, testCase := range cases {
		testCase := testCase
		t.Run(testCase.name, func(t *testing.T) {
			err := testCase.record.Validate()
			if testCase.wantErr && err == nil {
				t.Fatal("expected a validation error, got nil")
			}
			if !testCase.wantErr && err != nil {
				t.Fatalf("expected no validation error, got %v", err)
			}
		})
	}
}
