package main

// The `manifest` subcommand is a standalone utility for the encrypted cluster
// manifest — the canonical, durable record that deploy and teardown maintain on
// the automation host. It reuses the same AES-256-GCM Store, Record type, and
// wire format as the HTTP service, so the file the Go service serves live and
// the file inspected here are byte-for-byte the same contract.
//
// Because it shares the service's sources, the one binary does double duty: the
// baked linux-amd64 build inspects the manifest on a guest, and the same source
// compiled locally (`go build`) inspects the manifest on the Windows host.
//
// Usage:
//
//	inventory manifest show         -key KEY -file MANIFEST [-format table|json]
//	inventory manifest mark-removed -key KEY -file MANIFEST
//
// `show` decrypts and prints the manifest. `mark-removed` rewrites every record
// as Removed/Inactive (preserving last-known addresses) — the same transition
// teardown applies — so the record can be reconciled by hand if a teardown was
// interrupted.

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"text/tabwriter"
)

// manifestUsage describes the subcommand's invocation forms.
const manifestUsage = `usage:
  inventory manifest show         -key KEY -file MANIFEST [-format table|json]
  inventory manifest mark-removed -key KEY -file MANIFEST`

// manifestCommand runs the `manifest` subcommand and returns a process exit
// code. It never calls os.Exit itself, so it stays unit-testable.
func manifestCommand(arguments []string) int {
	if len(arguments) == 0 {
		fmt.Fprintln(os.Stderr, manifestUsage)
		return 2
	}

	action := arguments[0]
	switch action {
	case "show":
		return manifestShow(arguments[1:], os.Stdout)
	case "mark-removed":
		return manifestMarkRemoved(arguments[1:], os.Stdout)
	default:
		fmt.Fprintf(os.Stderr, "unknown manifest action %q\n%s\n", action, manifestUsage)
		return 2
	}
}

// manifestFlags parses the flags shared by every manifest action.
func manifestFlags(action string, arguments []string) (keyPath, filePath, format string, err error) {
	flagSet := flag.NewFlagSet("manifest "+action, flag.ContinueOnError)
	keyFlag := flagSet.String("key", "", "path to the base64 AES-256 key")
	fileFlag := flagSet.String("file", "", "path to the encrypted manifest file")
	formatFlag := flagSet.String("format", "table", "output format: table or json")
	if parseErr := flagSet.Parse(arguments); parseErr != nil {
		return "", "", "", parseErr
	}
	if *keyFlag == "" || *fileFlag == "" {
		return "", "", "", fmt.Errorf("both -key and -file are required")
	}
	return *keyFlag, *fileFlag, *formatFlag, nil
}

// manifestShow decrypts the manifest and prints it as a table or JSON.
func manifestShow(arguments []string, out io.Writer) int {
	keyPath, filePath, format, err := manifestFlags("show", arguments)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}

	records, err := loadManifestRecords(keyPath, filePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "manifest show: %v\n", err)
		return 1
	}

	switch format {
	case "json":
		return printManifestJSON(records, out)
	case "table":
		return printManifestTable(records, out)
	default:
		fmt.Fprintf(os.Stderr, "unknown -format %q (want table or json)\n", format)
		return 2
	}
}

// manifestMarkRemoved rewrites the manifest marking every host Removed/Inactive,
// preserving last-known addresses, mirroring what teardown does.
func manifestMarkRemoved(arguments []string, out io.Writer) int {
	keyPath, filePath, _, err := manifestFlags("mark-removed", arguments)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}

	key, err := LoadKey(keyPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "manifest mark-removed: %v\n", err)
		return 1
	}

	store := NewStore(filePath, key)
	records, err := store.Load()
	if err != nil {
		fmt.Fprintf(os.Stderr, "manifest mark-removed: %v\n", err)
		return 1
	}

	timestamp := utcTimestamp()
	for index := range records {
		records[index].Status = "Removed"
		records[index].StatusTimestamp = timestamp
		records[index].State = "Inactive"
		records[index].StateTimestamp = timestamp
	}

	if err := store.Save(records); err != nil {
		fmt.Fprintf(os.Stderr, "manifest mark-removed: %v\n", err)
		return 1
	}

	fmt.Fprintf(out, "Marked %d host(s) Removed/Inactive in %s\n", len(records), filePath)
	return 0
}

// loadManifestRecords loads and decrypts a manifest file.
func loadManifestRecords(keyPath, filePath string) ([]Record, error) {
	key, err := LoadKey(keyPath)
	if err != nil {
		return nil, err
	}
	return NewStore(filePath, key).Load()
}

// printManifestJSON writes the records as indented JSON.
func printManifestJSON(records []Record, out io.Writer) int {
	encoder := json.NewEncoder(out)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(records); err != nil {
		fmt.Fprintf(os.Stderr, "manifest show: cannot encode JSON: %v\n", err)
		return 1
	}
	return 0
}

// printManifestTable writes the records as an aligned text table.
func printManifestTable(records []Record, out io.Writer) int {
	if len(records) == 0 {
		fmt.Fprintln(out, "manifest is empty")
		return 0
	}

	writer := tabwriter.NewWriter(out, 0, 4, 2, ' ', 0)
	fmt.Fprintln(writer, "HOSTNAME\tROLE\tIPV4\tSTATUS\tSTATE\tUPDATED")
	for _, record := range records {
		fmt.Fprintf(
			writer, "%s\t%s\t%s\t%s\t%s\t%s\n",
			orDash(record.Hostname), orDash(record.Role), orDash(record.IPv4),
			orDash(record.Status), orDash(record.State), orDash(record.StateTimestamp),
		)
	}
	if err := writer.Flush(); err != nil {
		fmt.Fprintf(os.Stderr, "manifest show: cannot write table: %v\n", err)
		return 1
	}
	return 0
}

// orDash renders an empty field as a dash so columns never collapse.
func orDash(value string) string {
	if value == "" {
		return "-"
	}
	return value
}
