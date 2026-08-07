package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeManifestFixture creates a key file and an encrypted manifest holding the
// given records, and returns their paths.
func writeManifestFixture(t *testing.T, records []Record) (keyPath, filePath string) {
	t.Helper()
	directory := t.TempDir()

	keyPath = filepath.Join(directory, "inventory.key")
	encoded := base64.StdEncoding.EncodeToString(testKey())
	if err := os.WriteFile(keyPath, []byte(encoded), 0o600); err != nil {
		t.Fatalf("cannot write key fixture: %v", err)
	}

	filePath = filepath.Join(directory, "inventory.enc")
	if err := NewStore(filePath, testKey()).Save(records); err != nil {
		t.Fatalf("cannot write manifest fixture: %v", err)
	}
	return keyPath, filePath
}

func sampleRecords() []Record {
	return []Record{
		{Hostname: "apjump", Role: "jump", IPv4: "192.168.1.66", Status: "Deployed", State: "Active"},
		{Hostname: "apnode1", Role: "backend", IPv4: "192.168.1.67", Status: "Deployed", State: "Active"},
	}
}

func TestManifestShowTable(t *testing.T) {
	keyPath, filePath := writeManifestFixture(t, sampleRecords())

	var out bytes.Buffer
	code := manifestShow([]string{"-key", keyPath, "-file", filePath}, &out)
	if code != 0 {
		t.Fatalf("manifest show exited %d, want 0", code)
	}

	rendered := out.String()
	for _, want := range []string{"HOSTNAME", "apjump", "apnode1", "192.168.1.66"} {
		if !strings.Contains(rendered, want) {
			t.Errorf("table output missing %q:\n%s", want, rendered)
		}
	}
}

func TestManifestShowJSON(t *testing.T) {
	keyPath, filePath := writeManifestFixture(t, sampleRecords())

	var out bytes.Buffer
	code := manifestShow([]string{"-key", keyPath, "-file", filePath, "-format", "json"}, &out)
	if code != 0 {
		t.Fatalf("manifest show json exited %d, want 0", code)
	}

	var decoded []Record
	if err := json.Unmarshal(out.Bytes(), &decoded); err != nil {
		t.Fatalf("show json output is not valid JSON: %v", err)
	}
	if len(decoded) != 2 || decoded[0].Hostname != "apjump" {
		t.Fatalf("unexpected records from show json: %+v", decoded)
	}
}

func TestManifestMarkRemoved(t *testing.T) {
	keyPath, filePath := writeManifestFixture(t, sampleRecords())

	var out bytes.Buffer
	code := manifestMarkRemoved([]string{"-key", keyPath, "-file", filePath}, &out)
	if code != 0 {
		t.Fatalf("manifest mark-removed exited %d, want 0", code)
	}

	records, err := loadManifestRecords(keyPath, filePath)
	if err != nil {
		t.Fatalf("cannot reload manifest: %v", err)
	}
	for _, record := range records {
		if record.Status != "Removed" || record.State != "Inactive" {
			t.Errorf("host %s not marked removed: %+v", record.Hostname, record)
		}
		// Last-known address must be preserved for the audit trail.
		if record.IPv4 == "" {
			t.Errorf("host %s lost its last-known address", record.Hostname)
		}
	}
}

func TestManifestRequiresKeyAndFile(t *testing.T) {
	var out bytes.Buffer
	if code := manifestShow([]string{"-key", "only-key"}, &out); code != 2 {
		t.Fatalf("missing -file should exit 2, got %d", code)
	}
}

func TestManifestUnknownAction(t *testing.T) {
	if code := manifestCommand([]string{"frobnicate"}); code != 2 {
		t.Fatalf("unknown action should exit 2, got %d", code)
	}
}

func TestManifestNoAction(t *testing.T) {
	if code := manifestCommand(nil); code != 2 {
		t.Fatalf("no action should exit 2, got %d", code)
	}
}
