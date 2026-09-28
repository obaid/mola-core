// Package client speaks the guest half of the control-plane HTTP contract.
//
// The daemon holds exactly one credential at a time: a single-use registration
// token before it has registered, then a rotating machine token. It never holds
// hypervisor, billing or platform credentials, and it never learns anything
// about other tenants.
package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// Sentinel errors the daemon reacts to differently from a generic failure.
var (
	// ErrRegistrationUsed means the single-use token was already redeemed.
	// Either this is a replay, or state was lost and the machine must be
	// re-provisioned. Retrying will never help.
	ErrRegistrationUsed = errors.New("registration token already used")

	// ErrUnauthorized means the machine token was rejected. The daemon must
	// stop using it rather than hammering the control plane.
	ErrUnauthorized = errors.New("machine credential rejected")

	// ErrRecoveryChallenge means the control plane wants proof that we hold
	// the enrolment key before it will re-issue a credential. The challenge to
	// sign is carried on the error.
	ErrRecoveryChallenge = errors.New("recovery challenge issued")
)

// ChallengeError carries the nonce the control plane wants signed.
type ChallengeError struct {
	Challenge string
}

func (e *ChallengeError) Error() string { return "recovery challenge issued" }

func (e *ChallengeError) Unwrap() error { return ErrRecoveryChallenge }

// Client calls the control plane.
type Client struct {
	endpoint string
	http     *http.Client
}

// New builds a client for the given control-plane base URL.
func New(endpoint string, timeout time.Duration) *Client {
	return &Client{
		endpoint: strings.TrimRight(endpoint, "/"),
		http: &http.Client{
			Timeout: timeout,
		},
	}
}

// RegisterRequest is the body of POST /guest/register.
type RegisterRequest struct {
	RegistrationToken string `json:"registration_token"`
	MachineID         string `json:"machine_id"`
	GuestVersion      string `json:"guest_version"`
	OS                string `json:"os"`

	// EnrollmentPublicKey is the public half of a key generated and persisted
	// before this request. It binds the enrolment to this machine so a lost
	// response can be recovered by proving possession, not by restating
	// details an eavesdropper also saw.
	EnrollmentPublicKey string `json:"enrollment_public_key,omitempty"`

	// RecoveryChallenge and RecoverySignature answer a challenge from a prior
	// attempt. Empty on a first enrolment.
	RecoveryChallenge string `json:"recovery_challenge,omitempty"`
	RecoverySignature string `json:"recovery_signature,omitempty"`
}

// RegisterResponse is the control plane's answer to a successful registration.
type RegisterResponse struct {
	MachineToken      string `json:"machine_token"`
	ComputerID        string `json:"computer_id"`
	HeartbeatInterval int    `json:"heartbeat_interval"`

	// RecoveryChallenge is set on a 401 when the control plane wants proof of
	// possession before re-issuing a credential.
	RecoveryChallenge string `json:"recovery_challenge"`

	// Recovered is true when the control plane matched this machine to an
	// enrolment it had already redeemed — our previous success response was
	// lost in flight. Worth seeing in a boot log when diagnosing a flaky
	// network at provisioning time.
	Recovered bool `json:"recovered"`
}

// Sessions are activity hints used for auto-stop decisions only. They are
// never used for billing: the control plane meters host-side allocation.
type Sessions struct {
	SSH int `json:"ssh"`
	TTY int `json:"tty"`
}

// Capabilities is what this machine can currently do. The control plane will
// not call a computer ready without it — a recent heartbeat proves the daemon
// is alive, not that the computer works.
type Capabilities struct {
	Shell   bool             `json:"shell"`
	Display bool             `json:"display"`
	SSHD    bool             `json:"sshd"`
	Cua     *CuaCapabilities `json:"cua,omitempty"`
}

type CuaCapabilities struct {
	Installed bool   `json:"installed"`
	Version   string `json:"version"`
	Daemon    bool   `json:"daemon"`
}

// HeartbeatRequest is the body of POST /guest/heartbeat.
type HeartbeatRequest struct {
	UptimeSeconds int64    `json:"uptime_seconds"`
	Sessions      Sessions `json:"sessions"`
	Load          float64  `json:"load"`
	GuestVersion  string   `json:"guest_version"`

	// BootID identifies this boot. It is how the control plane distinguishes
	// the machine it just woke from the one it woke before.
	BootID string `json:"boot_id"`

	// RuntimeGeneration echoes what the control plane last told us, so a
	// daemon left over from a previous generation is visible as stale.
	RuntimeGeneration int64 `json:"runtime_generation,omitempty"`

	// ChallengeResponse answers the nonce from the previous beat, proving the
	// daemon is responding now rather than having responded once.
	ChallengeResponse string `json:"challenge_response,omitempty"`

	Capabilities Capabilities `json:"capabilities"`

	// ReportedIP is diagnostic only. The control plane records it for
	// operators and never dials it: a tenant is root in their own machine, so
	// an address they report is attacker-controlled input.
	ReportedIP string `json:"reported_ip,omitempty"`
}

// HeartbeatResponse carries the control plane's desired state back to the guest.
type HeartbeatResponse struct {
	DesiredState      string `json:"desired_state"`
	HeartbeatInterval int    `json:"heartbeat_interval"`
	// MachineToken is only present when the control plane rotated the
	// credential on this request. Empty means keep using the current one.
	MachineToken string `json:"machine_token"`

	// RuntimeGeneration is the generation the control plane believes this
	// machine is in. Echoed back on the next beat.
	RuntimeGeneration int64 `json:"runtime_generation"`

	// Challenge is a nonce to echo on the next heartbeat.
	Challenge string `json:"challenge"`

	// ChallengeVerified reports whether our previous answer was accepted.
	ChallengeVerified bool `json:"challenge_verified"`

	// AuthorizedKeys is the full set of keys permitted to log in, as the
	// control plane currently understands it. It is authoritative: a key
	// removed in the dashboard disappears from this list, and the guest
	// rewrites authorized_keys to match. Absent (nil) means the control
	// plane said nothing this beat and the current file stands.
	AuthorizedKeys []string `json:"authorized_keys"`
}

// Register exchanges the single-use registration token for a machine token.
func (c *Client) Register(ctx context.Context, req RegisterRequest) (*RegisterResponse, error) {
	var out RegisterResponse

	status, err := c.do(ctx, http.MethodPost, "/guest/register", "", req, &out)
	if err != nil {
		return nil, err
	}

	switch status {
	case http.StatusOK, http.StatusCreated:
		if out.MachineToken == "" {
			return nil, errors.New("control plane returned an empty machine token")
		}
		return &out, nil
	case http.StatusUnauthorized:
		// Either a challenge to sign, or a proof we failed. Only the former
		// carries a nonce.
		if out.RecoveryChallenge != "" {
			return nil, &ChallengeError{Challenge: out.RecoveryChallenge}
		}
		return nil, ErrUnauthorized
	case http.StatusConflict:
		return nil, ErrRegistrationUsed
	default:
		return nil, fmt.Errorf("register: unexpected status %d", status)
	}
}

// Heartbeat reports liveness and returns the control plane's desired state.
func (c *Client) Heartbeat(ctx context.Context, token string, req HeartbeatRequest) (*HeartbeatResponse, error) {
	var out HeartbeatResponse

	status, err := c.do(ctx, http.MethodPost, "/guest/heartbeat", token, req, &out)
	if err != nil {
		return nil, err
	}

	switch status {
	case http.StatusOK:
		return &out, nil
	case http.StatusUnauthorized, http.StatusForbidden:
		return nil, ErrUnauthorized
	default:
		return nil, fmt.Errorf("heartbeat: unexpected status %d", status)
	}
}

// ShutdownAck tells the control plane the guest accepted a shutdown request.
func (c *Client) ShutdownAck(ctx context.Context, token string) error {
	status, err := c.do(ctx, http.MethodPost, "/guest/shutdown-ack", token, struct{}{}, nil)
	if err != nil {
		return err
	}

	switch status {
	case http.StatusNoContent, http.StatusOK:
		return nil
	case http.StatusUnauthorized, http.StatusForbidden:
		return ErrUnauthorized
	default:
		return fmt.Errorf("shutdown-ack: unexpected status %d", status)
	}
}

// do performs one request, returning the status code and decoding the body
// into out when out is non-nil and the response looks like JSON.
func (c *Client) do(ctx context.Context, method, path, token string, body any, out any) (int, error) {
	payload, err := json.Marshal(body)
	if err != nil {
		return 0, fmt.Errorf("encode request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, method, c.endpoint+path, bytes.NewReader(payload))
	if err != nil {
		return 0, fmt.Errorf("build request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "mola-guest")

	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return 0, fmt.Errorf("call control plane: %w", err)
	}
	defer resp.Body.Close()

	// Bound the response so a hostile or broken endpoint cannot exhaust guest
	// memory.
	data, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return resp.StatusCode, fmt.Errorf("read response: %w", err)
	}

	// 401 is decoded too: a recovery challenge arrives on one, and losing the
	// nonce would leave the guest unable to prove possession.
	if out != nil && len(data) > 0 && (resp.StatusCode < 300 || resp.StatusCode == http.StatusUnauthorized) {
		if err := json.Unmarshal(data, out); err != nil {
			return resp.StatusCode, fmt.Errorf("decode response: %w", err)
		}
	}

	return resp.StatusCode, nil
}
