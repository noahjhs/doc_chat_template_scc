package config

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func testAuthDomain(server *httptest.Server) string {
	return strings.TrimPrefix(server.URL, "http://")
}

func TestLongPollPendingApprovalReturnsMatch(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/hosts/pending-approvals" {
			t.Fatalf("unexpected path %s", r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer dev-token" {
			t.Fatalf("unexpected Authorization header %q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"pending_approvals": []map[string]any{
				{"id": "abc123", "template_name": "npm scripts", "binary": "npm", "args": "run build", "host_label": "Erin's Mac"},
			},
		})
	}))
	defer server.Close()

	approval, err := LongPollPendingApproval(context.Background(), testAuthDomain(server), "dev-token")
	if err != nil {
		t.Fatalf("unexpected error: %s", err)
	}
	if approval == nil {
		t.Fatal("expected a non-nil approval")
	}
	if approval.ID != "abc123" || approval.Binary != "npm" || approval.Args != "run build" {
		t.Fatalf("unexpected approval: %+v", approval)
	}
}

func TestLongPollPendingApprovalEmptyIsNotAnError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"pending_approvals": []map[string]any{}})
	}))
	defer server.Close()

	approval, err := LongPollPendingApproval(context.Background(), testAuthDomain(server), "dev-token")
	if err != nil {
		t.Fatalf("a clean empty timeout should not be an error: %s", err)
	}
	if approval != nil {
		t.Fatalf("expected nil approval, got %+v", approval)
	}
}

func TestLongPollPendingApprovalUnexpectedStatusIsAnError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()

	if _, err := LongPollPendingApproval(context.Background(), testAuthDomain(server), "dev-token"); err == nil {
		t.Fatal("expected an error for a non-200 response")
	}
}

func TestPostPendingApprovalDecisionSendsExpectedBody(t *testing.T) {
	var gotPath, gotDecision string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		var body struct {
			Decision string `json:"decision"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		gotDecision = body.Decision
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"id": "abc123", "decision": gotDecision})
	}))
	defer server.Close()

	if err := PostPendingApprovalDecision(testAuthDomain(server), "dev-token", "abc123", true); err != nil {
		t.Fatalf("unexpected error: %s", err)
	}
	if gotPath != "/hosts/pending-approvals/abc123/decision" {
		t.Fatalf("unexpected path %s", gotPath)
	}
	if gotDecision != "allow" {
		t.Fatalf("expected decision %q, got %q", "allow", gotDecision)
	}
}

func TestPostPendingApprovalDecisionErrorsOnRejection(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer server.Close()

	if err := PostPendingApprovalDecision(testAuthDomain(server), "dev-token", "abc123", false); err == nil {
		t.Fatal("expected an error for a 403 response")
	}
}
