package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHandleRootRedirectsToClusterView(t *testing.T) {
	srv := &server{}
	request := httptest.NewRequest(http.MethodGet, "/", nil)
	recorder := httptest.NewRecorder()

	srv.handleRoot(recorder, request)

	if recorder.Code != http.StatusFound {
		t.Fatalf("root should redirect (302), got %d", recorder.Code)
	}
	if location := recorder.Header().Get("Location"); location != "/clusterview" {
		t.Fatalf("root should redirect to /clusterview, got %q", location)
	}
}

func TestHandleRootNotFoundForOtherPaths(t *testing.T) {
	srv := &server{}
	request := httptest.NewRequest(http.MethodGet, "/nope", nil)
	recorder := httptest.NewRecorder()

	srv.handleRoot(recorder, request)

	if recorder.Code != http.StatusNotFound {
		t.Fatalf("unmatched path should 404, got %d", recorder.Code)
	}
}
