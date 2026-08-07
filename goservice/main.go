// Command inventory serves the encrypted cluster host registry.
//
// It exposes a decrypted JSON view, an HTML cluster view, and the raw
// ciphertext, so an external test suite can prove three separate properties:
// that the service decrypts correctly, that it renders what it decrypted, and
// that the bytes at rest are genuinely encrypted rather than merely encoded.
//
// Endpoints:
//
//	GET  /healthz            liveness probe
//	GET  /clusterview        HTML table of the decrypted registry
//	GET  /api/inventory      decrypted registry as JSON
//	GET  /api/inventory/raw  the datastore exactly as stored, still encrypted
//	POST /api/inventory      insert or replace one host record
package main

import (
	"bytes"
	"context"
	"embed"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"html/template"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"
)

//go:embed templates/clusterview.html
var templateFS embed.FS

const (
	readHeaderTimeout = 10 * time.Second
	writeTimeout      = 30 * time.Second
	idleTimeout       = 60 * time.Second
	shutdownGrace     = 10 * time.Second
	maxRequestBytes   = 1 << 20
)

// server wires the datastore to the HTTP handlers.
type server struct {
	store        *Store
	pageTemplate *template.Template
}

// viewData is the model passed to the cluster view template.
type viewData struct {
	Records  []Record
	Rendered string
}

func main() {
	// Subcommand dispatch: with no subcommand the binary runs as the HTTP
	// service (its primary role); `manifest` turns the same binary into the
	// offline manifest utility, reusing the identical crypto and record types.
	if len(os.Args) > 1 && os.Args[1] == "manifest" {
		os.Exit(manifestCommand(os.Args[2:]))
	}

	listenAddr := flag.String("addr", ":5090", "listen address")
	keyPath := flag.String("key", "/opt/inventory/inventory.key", "path to base64 AES-256 key")
	storePath := flag.String("store", "/opt/inventory/inventory.enc", "path to encrypted datastore")
	flag.Parse()

	key, err := LoadKey(*keyPath)
	if err != nil {
		log.Fatalf("inventory: %v", err)
	}

	if err := os.MkdirAll(filepath.Dir(*storePath), 0o750); err != nil {
		log.Fatalf("inventory: cannot create datastore directory: %v", err)
	}

	pageTemplate, err := template.ParseFS(templateFS, "templates/clusterview.html")
	if err != nil {
		log.Fatalf("inventory: cannot parse embedded template: %v", err)
	}

	srv := &server{store: NewStore(*storePath, key), pageTemplate: pageTemplate}

	mux := http.NewServeMux()
	mux.HandleFunc("/", srv.handleRoot)
	mux.HandleFunc("/healthz", srv.handleHealth)
	mux.HandleFunc("/clusterview", srv.handleClusterView)
	mux.HandleFunc("/api/inventory", srv.handleInventory)
	mux.HandleFunc("/api/inventory/raw", srv.handleRaw)

	httpServer := &http.Server{
		Addr:              *listenAddr,
		Handler:           mux,
		ReadHeaderTimeout: readHeaderTimeout,
		WriteTimeout:      writeTimeout,
		IdleTimeout:       idleTimeout,
	}

	go func() {
		log.Printf("inventory: listening on %s (datastore %s)", *listenAddr, *storePath)
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("inventory: %v", err)
		}
	}()

	// systemd stops the unit with SIGTERM; draining in-flight requests avoids
	// truncated responses during a redeploy.
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop

	log.Println("inventory: shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownGrace)
	defer cancel()
	if err := httpServer.Shutdown(shutdownCtx); err != nil {
		log.Printf("inventory: graceful shutdown failed: %v", err)
	}
}

// handleRoot redirects the site root to the human-readable cluster view and
// returns 404 for any other unmatched path. Without it, hitting the service at
// "/" returned a bare 404, which reads like an outage rather than "you want
// /clusterview".
func (srv *server) handleRoot(writer http.ResponseWriter, request *http.Request) {
	if request.URL.Path != "/" {
		http.NotFound(writer, request)
		return
	}
	http.Redirect(writer, request, "/clusterview", http.StatusFound)
}

// handleHealth reports liveness without touching the datastore, so the probe
// still succeeds while the registry is empty or being rewritten.
func (srv *server) handleHealth(writer http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodGet {
		methodNotAllowed(writer, http.MethodGet)
		return
	}
	writer.Header().Set("Content-Type", "text/plain; charset=utf-8")
	writer.WriteHeader(http.StatusOK)
	if _, err := writer.Write([]byte("ok\n")); err != nil {
		log.Printf("inventory: health write failed: %v", err)
	}
}

// handleClusterView renders the decrypted registry as HTML.
func (srv *server) handleClusterView(writer http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodGet {
		methodNotAllowed(writer, http.MethodGet)
		return
	}

	records, err := srv.store.Load()
	if err != nil {
		srv.fail(writer, err)
		return
	}

	// Render into a buffer first: writing straight to the ResponseWriter would
	// commit a 200 status and partial markup before a mid-template error could
	// be reported, leaving the client with a silently truncated page.
	var rendered bytes.Buffer
	data := viewData{Records: records, Rendered: utcTimestamp()}
	if err := srv.pageTemplate.ExecuteTemplate(&rendered, "clusterview.html", data); err != nil {
		srv.fail(writer, fmt.Errorf("cannot render cluster view: %w", err))
		return
	}

	writer.Header().Set("Content-Type", "text/html; charset=utf-8")
	writer.WriteHeader(http.StatusOK)
	if _, err := writer.Write(rendered.Bytes()); err != nil {
		log.Printf("inventory: cluster view write failed: %v", err)
	}
}

// handleInventory serves the decrypted registry and accepts record upserts.
func (srv *server) handleInventory(writer http.ResponseWriter, request *http.Request) {
	switch request.Method {
	case http.MethodGet:
		records, err := srv.store.Load()
		if err != nil {
			srv.fail(writer, err)
			return
		}
		writeJSON(writer, http.StatusOK, records)

	case http.MethodPost:
		var candidate Record
		decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, maxRequestBytes))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&candidate); err != nil {
			writeJSON(writer, http.StatusBadRequest, map[string]string{
				"error": "request body is not a valid inventory record: " + err.Error(),
			})
			return
		}
		if err := candidate.Validate(); err != nil {
			writeJSON(writer, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}

		if candidate.StatusTimestamp == "" {
			candidate.StatusTimestamp = utcTimestamp()
		}
		if candidate.StateTimestamp == "" {
			candidate.StateTimestamp = utcTimestamp()
		}

		records, err := srv.store.Upsert(candidate)
		if err != nil {
			srv.fail(writer, err)
			return
		}
		writeJSON(writer, http.StatusOK, records)

	default:
		methodNotAllowed(writer, http.MethodGet, http.MethodPost)
	}
}

// handleRaw serves the datastore exactly as stored, still encrypted.
func (srv *server) handleRaw(writer http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodGet {
		methodNotAllowed(writer, http.MethodGet)
		return
	}

	blob, err := srv.store.RawCiphertext()
	if err != nil {
		srv.fail(writer, err)
		return
	}

	writer.Header().Set("Content-Type", "text/plain; charset=utf-8")
	writer.Header().Set("X-Content-Encoding", "aes-256-gcm+base64")
	writer.WriteHeader(http.StatusOK)
	if _, err := writer.Write(blob); err != nil {
		log.Printf("inventory: raw write failed: %v", err)
	}
}

// fail maps an internal error onto a response, distinguishing decryption
// failure so operators can tell a key or tampering problem from an I/O one.
func (srv *server) fail(writer http.ResponseWriter, err error) {
	log.Printf("inventory: %v", err)
	if errors.Is(err, ErrDecryptionFailed) {
		writeJSON(writer, http.StatusInternalServerError, map[string]string{
			"error": "inventory decryption failed",
			"hint":  "the service key does not match the datastore, or the datastore was altered",
		})
		return
	}
	writeJSON(writer, http.StatusInternalServerError, map[string]string{"error": err.Error()})
}

// writeJSON serialises a payload with a status code.
func writeJSON(writer http.ResponseWriter, status int, payload interface{}) {
	writer.Header().Set("Content-Type", "application/json; charset=utf-8")
	writer.WriteHeader(status)
	if err := json.NewEncoder(writer).Encode(payload); err != nil {
		log.Printf("inventory: JSON write failed: %v", err)
	}
}

// methodNotAllowed rejects a request and advertises the permitted methods.
func methodNotAllowed(writer http.ResponseWriter, allowed ...string) {
	for _, method := range allowed {
		writer.Header().Add("Allow", method)
	}
	writeJSON(writer, http.StatusMethodNotAllowed, map[string]string{
		"error": "method not allowed",
	})
}
