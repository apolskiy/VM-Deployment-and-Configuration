package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Record describes one host in the cluster registry.
//
// The JSON field names are the contract shared with the Python client; they
// must not be renamed on one side alone.
type Record struct {
	Hostname        string `json:"hostname"`
	Role            string `json:"role"`
	IPv4            string `json:"ipv4"`
	IPv6            string `json:"ipv6"`
	Status          string `json:"status"`
	StatusTimestamp string `json:"status_timestamp"`
	State           string `json:"state"`
	StateTimestamp  string `json:"state_timestamp"`
}

// Validate reports whether the record carries the minimum identifying fields.
func (record Record) Validate() error {
	if record.Hostname == "" {
		return fmt.Errorf("record requires a non-empty hostname")
	}
	if record.Role != "jump" && record.Role != "backend" {
		return fmt.Errorf("record role must be 'jump' or 'backend', got %q", record.Role)
	}
	return nil
}

// Store is a concurrency-safe, file-backed encrypted registry.
//
// Every read decrypts from disk rather than serving a cached copy, because the
// Python provisioning tooling writes the same file out of band; a cache would
// serve stale membership immediately after a deployment.
type Store struct {
	mutex sync.RWMutex
	path  string
	key   []byte
}

// NewStore binds a store to a datastore path and encryption key.
func NewStore(path string, key []byte) *Store {
	return &Store{path: path, key: key}
}

// Load decrypts and returns the current registry contents.
//
// A missing datastore is not an error: it is the legitimate state of a cluster
// before the first host registers, and returns an empty slice.
func (store *Store) Load() ([]Record, error) {
	store.mutex.RLock()
	defer store.mutex.RUnlock()
	return store.loadLocked()
}

// loadLocked performs the read. Callers must already hold the lock.
func (store *Store) loadLocked() ([]Record, error) {
	blob, err := os.ReadFile(store.path)
	if os.IsNotExist(err) {
		return []Record{}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("cannot read datastore %s: %w", store.path, err)
	}
	if len(blob) == 0 {
		return []Record{}, nil
	}

	plaintext, err := Decrypt(blob, store.key)
	if err != nil {
		return nil, err
	}

	var records []Record
	if err := json.Unmarshal(plaintext, &records); err != nil {
		return nil, fmt.Errorf("decrypted inventory is not a valid JSON array: %w", err)
	}
	return records, nil
}

// Save encrypts and atomically replaces the datastore.
func (store *Store) Save(records []Record) error {
	store.mutex.Lock()
	defer store.mutex.Unlock()
	return store.saveLocked(records)
}

// saveLocked performs the write. Callers must already hold the lock.
//
// The payload is written to a temporary file in the same directory and then
// renamed. Rename within a directory is atomic on POSIX, so a reader can never
// observe a half-written datastore, which under GCM would fail authentication
// and look identical to tampering.
func (store *Store) saveLocked(records []Record) error {
	if records == nil {
		records = []Record{}
	}

	plaintext, err := json.MarshalIndent(records, "", "  ")
	if err != nil {
		return fmt.Errorf("cannot serialise inventory records: %w", err)
	}

	sealed, err := Encrypt(plaintext, store.key)
	if err != nil {
		return err
	}

	directory := filepath.Dir(store.path)
	tempFile, err := os.CreateTemp(directory, ".inventory-*.tmp")
	if err != nil {
		return fmt.Errorf("cannot create temporary datastore in %s: %w", directory, err)
	}
	tempName := tempFile.Name()

	if _, err := tempFile.Write(sealed); err != nil {
		tempFile.Close()
		os.Remove(tempName)
		return fmt.Errorf("cannot write temporary datastore: %w", err)
	}
	if err := tempFile.Sync(); err != nil {
		tempFile.Close()
		os.Remove(tempName)
		return fmt.Errorf("cannot flush temporary datastore: %w", err)
	}
	if err := tempFile.Close(); err != nil {
		os.Remove(tempName)
		return fmt.Errorf("cannot close temporary datastore: %w", err)
	}
	// 0660, not 0600: the service runs as a systemd DynamicUser whose UID can
	// change across restarts, so the datastore must stay readable and writable
	// by the shared vmdeploy-inventory group (which the setgid parent directory
	// assigns to this file) rather than only by the creating user.
	if err := os.Chmod(tempName, 0o660); err != nil {
		os.Remove(tempName)
		return fmt.Errorf("cannot restrict datastore permissions: %w", err)
	}
	if err := os.Rename(tempName, store.path); err != nil {
		os.Remove(tempName)
		return fmt.Errorf("cannot replace datastore %s: %w", store.path, err)
	}
	return nil
}

// Upsert inserts a record, replacing any existing entry with the same
// hostname while preserving its position so member ordering stays stable.
func (store *Store) Upsert(candidate Record) ([]Record, error) {
	store.mutex.Lock()
	defer store.mutex.Unlock()

	records, err := store.loadLocked()
	if err != nil {
		return nil, err
	}

	replaced := false
	for index := range records {
		if records[index].Hostname == candidate.Hostname {
			records[index] = candidate
			replaced = true
			break
		}
	}
	if !replaced {
		records = append(records, candidate)
	}

	if err := store.saveLocked(records); err != nil {
		return nil, err
	}
	return records, nil
}

// RawCiphertext returns the datastore exactly as stored, still encrypted.
//
// This backs the endpoint the test suite uses to prove that data at rest is
// genuinely ciphertext and not merely encoded.
func (store *Store) RawCiphertext() ([]byte, error) {
	store.mutex.RLock()
	defer store.mutex.RUnlock()

	blob, err := os.ReadFile(store.path)
	if os.IsNotExist(err) {
		return []byte{}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("cannot read datastore %s: %w", store.path, err)
	}
	return blob, nil
}

// utcTimestamp renders the current time in the format the Python client uses.
func utcTimestamp() string {
	return time.Now().UTC().Format("2006-01-02T15:04:05Z")
}
