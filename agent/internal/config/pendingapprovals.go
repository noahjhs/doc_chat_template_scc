package config

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// PendingApproval mirrors auth_service's PendingApprovalInfo -- one
// "ask"-tier command-template call awaiting a human decision, relayed here
// via LongPollPendingApproval so it can be answered with a native dialog
// instead of only ever through the browser's in-chat Approve/Deny UI.
type PendingApproval struct {
	ID           string `json:"id"`
	TemplateName string `json:"template_name"`
	Binary       string `json:"binary"`
	Args         string `json:"args"`
	HostLabel    string `json:"host_label"`
}

// LongPollPendingApproval makes one long-poll attempt against GET
// /hosts/pending-approvals, which waits server-side (~25s) for a pending
// approval belonging to a user this device is currently the attended host
// for, before returning. A nil result with a nil error is the normal
// "nothing showed up this round" outcome, not an error -- the caller should
// just call again immediately. The client timeout (35s) is set well above
// the server's own hold so a slow-but-healthy long-poll is never mistaken
// for a network failure that should back off. Takes ctx (unlike
// FetchCommandTemplates's plain http.NewRequest) so a daemon shutdown can
// cancel an in-flight ~25s wait promptly instead of blocking exit.
func LongPollPendingApproval(ctx context.Context, authDomain, deviceToken string) (*PendingApproval, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, BaseURL(authDomain)+"/hosts/pending-approvals", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+deviceToken)
	client := &http.Client{Timeout: 35 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("unexpected status %d long-polling pending approvals", resp.StatusCode)
	}
	var body struct {
		PendingApprovals []PendingApproval `json:"pending_approvals"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	if len(body.PendingApprovals) == 0 {
		return nil, nil
	}
	return &body.PendingApprovals[0], nil
}

// PostPendingApprovalDecision reports a human's Allow/Deny decision back --
// only accepted server-side if this device is still the current attended
// host for that approval's user (see auth_service/main.py's
// decide_pending_approval); a decision from a since-un-designated device is
// rejected there, surfaced here as a non-nil error.
func PostPendingApprovalDecision(authDomain, deviceToken, approvalID string, allow bool) error {
	decision := "deny"
	if allow {
		decision = "allow"
	}
	payload, err := json.Marshal(map[string]string{"decision": decision})
	if err != nil {
		return err
	}
	req, err := http.NewRequest(
		http.MethodPost,
		BaseURL(authDomain)+"/hosts/pending-approvals/"+approvalID+"/decision",
		bytes.NewReader(payload),
	)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+deviceToken)
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("unexpected status %d posting approval decision", resp.StatusCode)
	}
	return nil
}
