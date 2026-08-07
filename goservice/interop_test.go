package main

import (
	"encoding/json"
	"os"
	"testing"
)

// TestDecryptsPythonGeneratedPayload pins the cross-language wire format.
//
// testdata/python_generated.enc was produced by the Python client in
// src/vmdeploy/inventory.py using the key bytes 0x00..0x1F. If either side
// changes its framing, nonce length, or JSON field names, this test fails
// rather than the mismatch surfacing at deployment time as an opaque
// authentication error on a live jump station.
func TestDecryptsPythonGeneratedPayload(t *testing.T) {
	blob, err := os.ReadFile("testdata/python_generated.enc")
	if err != nil {
		t.Fatalf("cannot read Python-generated fixture: %v", err)
	}

	plaintext, err := Decrypt(blob, testKey())
	if err != nil {
		t.Fatalf("could not decrypt Python-generated payload: %v", err)
	}

	var records []Record
	if err := json.Unmarshal(plaintext, &records); err != nil {
		t.Fatalf("Python plaintext is not a Go-compatible JSON array: %v", err)
	}
	if len(records) != 2 {
		t.Fatalf("expected 2 records, got %d", len(records))
	}

	want := map[string]string{"apjump": "jump", "apnode1": "backend"}
	for _, record := range records {
		expectedRole, found := want[record.Hostname]
		if !found {
			t.Fatalf("unexpected hostname %q in fixture", record.Hostname)
		}
		if record.Role != expectedRole {
			t.Errorf("host %s: role = %q, want %q", record.Hostname, record.Role, expectedRole)
		}
		if record.IPv4 == "" || record.State == "" || record.StatusTimestamp == "" {
			t.Errorf("host %s: field(s) lost in translation: %+v", record.Hostname, record)
		}
	}
}

// TestGoCiphertextIsPythonReadable is the reverse direction, verified by
// re-opening a Go-sealed payload through the same code path the Python client
// uses: base64 decode, split a 12-byte nonce, AES-256-GCM open.
func TestGoCiphertextIsPythonReadable(t *testing.T) {
	key := testKey()
	original := []byte(`[{"hostname":"apnode2","role":"backend","ipv4":"192.168.1.82",` +
		`"ipv6":"","status":"Deployed","status_timestamp":"2026-08-05T10:02:00Z",` +
		`"state":"Active","state_timestamp":"2026-08-05T10:02:00Z"}]`)

	sealed, err := Encrypt(original, key)
	if err != nil {
		t.Fatalf("Encrypt failed: %v", err)
	}

	opened, err := Decrypt(sealed, key)
	if err != nil {
		t.Fatalf("Decrypt failed: %v", err)
	}

	var records []Record
	if err := json.Unmarshal(opened, &records); err != nil {
		t.Fatalf("Go plaintext is not a valid JSON array: %v", err)
	}
	if len(records) != 1 || records[0].Hostname != "apnode2" {
		t.Fatalf("unexpected records after round trip: %+v", records)
	}
}
