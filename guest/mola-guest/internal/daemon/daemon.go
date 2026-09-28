// Package daemon runs the guest agent: register once, then heartbeat forever,
// obeying whatever desired state the control plane reports back.
package daemon

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"math/rand"
	"os/exec"
	"time"

	"github.com/obaid/mola-core/guest/mola-guest/internal/capabilities"
	"github.com/obaid/mola-core/guest/mola-guest/internal/client"
	"github.com/obaid/mola-core/guest/mola-guest/internal/config"
	"github.com/obaid/mola-core/guest/mola-guest/internal/sessions"
)

// Capabilities probes what the machine can currently do. Injectable so tests
// do not need a real display or sshd.
type Capabilities interface {
	Probe(ctx context.Context) capabilities.Report
}

// Activity is the subset of session reading the daemon needs. Injectable so
// tests can drive activity without a real /proc.
type Activity interface {
	Read() sessions.Counts
	LoadAverage() float64
	Uptime() int64
}

// Shutdowner performs the local shutdown when the control plane asks for it.
type Shutdowner interface {
	Shutdown(ctx context.Context) error
}

// CommandShutdowner shells out to the configured shutdown command.
type CommandShutdowner struct {
	Command []string
}

// Shutdown runs the configured command.
func (c CommandShutdowner) Shutdown(ctx context.Context) error {
	if len(c.Command) == 0 {
		return errors.New("no shutdown command configured")
	}

	cmd := exec.CommandContext(ctx, c.Command[0], c.Command[1:]...)

	return cmd.Run()
}

// Daemon is the guest agent.
type Daemon struct {
	cfg      *config.Config
	client   *client.Client
	store    *Store
	activity Activity
	capabil  Capabilities
	shutdown Shutdowner
	log      *slog.Logger

	// generation and challenge carry control-plane state between beats so the
	// next heartbeat can prove it is answering now.
	generation int64
	challenge  string

	// interval is the current heartbeat cadence; the control plane can change
	// it in any response.
	interval time.Duration

	// failures counts consecutive heartbeat failures, driving backoff.
	failures int

	// sleep is injectable so tests do not wait in real time.
	sleep func(ctx context.Context, d time.Duration)

	// jitter returns a fraction in [0,1) used to spread heartbeats out. A
	// fleet that all boots at once must not heartbeat in lockstep.
	jitter func() float64
}

// Options configures a Daemon.
type Options struct {
	Config       *config.Config
	Client       *client.Client
	Store        *Store
	Activity     Activity
	Capabilities Capabilities
	Shutdown     Shutdowner
	Logger       *slog.Logger
}

// New builds a daemon from options, filling in production defaults.
func New(opts Options) *Daemon {
	d := &Daemon{
		cfg:      opts.Config,
		client:   opts.Client,
		store:    opts.Store,
		activity: opts.Activity,
		capabil:  opts.Capabilities,
		shutdown: opts.Shutdown,
		log:      opts.Logger,
		interval: opts.Config.HeartbeatInterval,
		sleep:    sleepCtx,
		jitter:   rand.Float64,
	}

	if d.log == nil {
		d.log = slog.Default()
	}

	if d.activity == nil {
		d.activity = sessions.NewReader("/")
	}

	if d.capabil == nil {
		d.capabil = capabilities.NewProber()
	}

	if d.shutdown == nil {
		d.shutdown = CommandShutdowner{Command: opts.Config.ShutdownCommand}
	}

	return d
}

// maxBackoff caps the retry delay. The control plane being briefly unreachable
// must not turn into a machine that never reports again, so backoff is bounded.
const maxBackoff = 2 * time.Minute

// maxRecoveryAttempts bounds the challenge/response exchange during enrolment.
const maxRecoveryAttempts = 3

// Run registers if needed and then heartbeats until the context is cancelled.
func (d *Daemon) Run(ctx context.Context) error {
	if err := d.ensureRegistered(ctx); err != nil {
		return err
	}

	d.log.Info("guest daemon ready",
		"computer_id", d.store.ComputerID(),
		"version", d.cfg.Version,
		"interval", d.interval,
	)

	needsRegistration := false
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}

		if needsRegistration {
			if err := d.ensureRegistered(ctx); err != nil {
				if ctx.Err() != nil {
					return nil
				}
				d.failures++
				delay := d.backoff()
				d.log.Warn("registration recovery failed", "error", err, "retry_in", delay)
				d.sleep(ctx, delay)
				continue
			}
			needsRegistration = false
		}

		stop, err := d.beat(ctx)
		if err != nil {
			// A restored disk carries an old bearer credential, but this boot's
			// config already contains the fresh identity seed. Clear only the
			// rejected token and re-enroll in this process: the entrypoint does
			// not restart an exited daemon while the desktop remains running.
			if errors.Is(err, client.ErrUnauthorized) {
				d.log.Warn("machine credential rejected, recovering registration")
				if err := d.store.Clear(); err != nil {
					return fmt.Errorf("clear rejected machine credential: %w", err)
				}
				needsRegistration = true
				d.failures++
				d.sleep(ctx, d.backoff())
				continue
			}

			d.failures++
			delay := d.backoff()
			d.log.Warn("heartbeat failed", "error", err, "attempt", d.failures, "retry_in", delay)
			d.sleep(ctx, delay)

			continue
		}

		d.failures = 0

		if stop {
			return d.performShutdown(ctx)
		}

		d.sleep(ctx, d.nextInterval())
	}
}

// ensureRegistered exchanges the single-use registration token for a machine
// token, unless a credential is already on the persistent volume.
func (d *Daemon) ensureRegistered(ctx context.Context) error {
	// A restored snapshot or a fork boots with the *source* machine's
	// credential sitting on its disk. Using it would authenticate this machine
	// as the one it was copied from. When the bootstrap tells us which
	// computer we are and the stored state disagrees, the state belongs to a
	// previous life and must go.
	if d.cfg.ComputerID != "" {
		if stored := d.store.ComputerID(); stored != "" && stored != d.cfg.ComputerID {
			d.log.Warn("stored identity belongs to another computer, discarding",
				"stored", stored, "assigned", d.cfg.ComputerID)

			if err := d.store.ClearIdentity(); err != nil {
				return fmt.Errorf("discard carried-over identity: %w", err)
			}
		}
	}

	if d.store.MachineToken() != "" {
		d.log.Info("reusing machine credential from persistent state")

		return nil
	}

	if d.cfg.RegistrationToken == "" {
		return errors.New("no machine credential and no registration token: machine cannot register")
	}

	machineID, err := d.store.MachineID(d.cfg.MachineID)
	if err != nil {
		return err
	}

	// Established before we ever speak to the control plane. If our success
	// response is lost, this key is the only thing that distinguishes us from
	// someone who watched the exchange.
	signingKey, err := d.store.EnrollmentKey()
	if err != nil {
		return err
	}

	publicKey := base64.StdEncoding.EncodeToString(signingKey.Public().(ed25519.PublicKey))

	attempt := 0

	// Carried between attempts when the control plane asks us to prove
	// possession before re-issuing a credential.
	var challenge, signature string

	recoveries := 0

	for {
		resp, err := d.client.Register(ctx, client.RegisterRequest{
			RegistrationToken:   d.cfg.RegistrationToken,
			MachineID:           machineID,
			GuestVersion:        d.cfg.Version,
			OS:                  d.cfg.OS,
			EnrollmentPublicKey: publicKey,
			RecoveryChallenge:   challenge,
			RecoverySignature:   signature,
		})

		if err == nil {
			if err := d.store.SetMachineToken(resp.MachineToken); err != nil {
				return err
			}

			if err := d.store.SetComputerID(resp.ComputerID); err != nil {
				return err
			}

			if resp.HeartbeatInterval > 0 {
				d.interval = time.Duration(resp.HeartbeatInterval) * time.Second
			}

			if resp.Recovered {
				// Our previous attempt succeeded server-side and the answer
				// never reached us. The machine id we persisted before asking
				// is what let the control plane recognise us rather than treat
				// the retry as a clone replaying a stolen token.
				d.log.Info("recovered enrollment after lost response", "computer_id", resp.ComputerID)
			} else {
				d.log.Info("registered with control plane", "computer_id", resp.ComputerID)
			}

			return nil
		}

		// The control plane redeemed this token before and wants proof we are
		// the machine that enrolled. Sign the nonce and come straight back.
		var challengeErr *client.ChallengeError
		if errors.As(err, &challengeErr) {
			// Bounded: a control plane that keeps challenging a proof it will
			// not accept must not spin the daemon forever.
			recoveries++
			if recoveries > maxRecoveryAttempts {
				return fmt.Errorf("enrollment recovery rejected after %d attempts", recoveries-1)
			}

			challenge = challengeErr.Challenge
			signature = base64.StdEncoding.EncodeToString(
				ed25519.Sign(signingKey, []byte(challenge)),
			)

			d.log.Info("proving possession of enrollment key to recover")

			continue
		}

		// A spent token belonging to a *different* machine can never succeed.
		// Retrying is pointless and looks like an attack from the control
		// plane's side. Our own lost-response case is not this: it comes back
		// as a success with Recovered set, because the machine id we persisted
		// before the first request identifies us.
		if errors.Is(err, client.ErrRegistrationUsed) {
			return fmt.Errorf("registration token already used: %w", err)
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		attempt++
		delay := backoffFor(attempt)
		d.log.Warn("registration failed", "error", err, "attempt", attempt, "retry_in", delay)
		d.sleep(ctx, delay)
	}
}

// beat sends one heartbeat. It returns true when the control plane wants the
// machine stopped.
func (d *Daemon) beat(ctx context.Context) (bool, error) {
	token := d.store.MachineToken()
	if token == "" {
		return false, client.ErrUnauthorized
	}

	counts := d.activity.Read()
	caps := d.capabil.Probe(ctx)
	var cua *client.CuaCapabilities
	if caps.Cua != nil {
		cua = &client.CuaCapabilities{
			Installed: caps.Cua.Installed,
			Version:   caps.Cua.Version,
			Daemon:    caps.Cua.Daemon,
		}
	}

	resp, err := d.client.Heartbeat(ctx, token, client.HeartbeatRequest{
		UptimeSeconds: d.activity.Uptime(),
		Sessions: client.Sessions{
			SSH: counts.SSH,
			TTY: counts.TTY,
		},
		Load:         d.activity.LoadAverage(),
		GuestVersion: d.cfg.Version,

		// Readiness evidence. A heartbeat alone says the daemon is alive; this
		// says which boot is alive and whether the computer actually works.
		BootID:            capabilities.BootID(),
		RuntimeGeneration: d.generation,
		ChallengeResponse: d.challenge,
		Capabilities: client.Capabilities{
			Shell:   caps.Shell,
			Display: caps.Display,
			SSHD:    caps.SSHD,
			Cua:     cua,
		},
	})
	if err != nil {
		return false, err
	}

	// Carry the control plane's state into the next beat.
	d.generation = resp.RuntimeGeneration
	d.challenge = resp.Challenge

	// The control plane rotates the credential by handing back a new one.
	if resp.MachineToken != "" && resp.MachineToken != token {
		if err := d.store.SetMachineToken(resp.MachineToken); err != nil {
			return false, fmt.Errorf("persist rotated credential: %w", err)
		}

		d.log.Info("machine credential rotated")
	}

	if resp.HeartbeatInterval > 0 {
		d.interval = time.Duration(resp.HeartbeatInterval) * time.Second
	}

	// Keys are injected at machine creation, so without this a key added or
	// revoked later would never reach a machine that already exists.
	if changed, err := syncAuthorizedKeys(d.cfg.AuthorizedKeysPath, resp.AuthorizedKeys); err != nil {
		// Never fatal: losing SSH key sync must not take the daemon down and
		// with it the heartbeat that billing and auto-stop depend on.
		d.log.Error("could not sync authorized keys", "error", err)
	} else if changed {
		d.log.Info("authorized keys updated", "count", len(resp.AuthorizedKeys))
	}

	return resp.DesiredState == "stopped", nil
}

// performShutdown acknowledges the request, then shuts the guest down.
//
// The acknowledgement is best-effort and never blocks the shutdown: the
// control plane's stop path has its own timeout and will force-stop if the
// machine does not go away.
func (d *Daemon) performShutdown(ctx context.Context) error {
	d.log.Info("control plane requested shutdown")

	if token := d.store.MachineToken(); token != "" {
		if err := d.client.ShutdownAck(ctx, token); err != nil {
			d.log.Warn("shutdown ack failed", "error", err)
		}
	}

	if err := d.shutdown.Shutdown(ctx); err != nil {
		return fmt.Errorf("shutdown: %w", err)
	}

	return nil
}

// nextInterval spreads heartbeats by up to 20% so a fleet does not synchronise.
func (d *Daemon) nextInterval() time.Duration {
	spread := float64(d.interval) * 0.2 * d.jitter()

	return d.interval + time.Duration(spread)
}

func (d *Daemon) backoff() time.Duration {
	return backoffFor(d.failures)
}

// backoffFor is exponential with a hard ceiling.
func backoffFor(attempt int) time.Duration {
	if attempt < 1 {
		attempt = 1
	}

	if attempt > 10 {
		attempt = 10
	}

	delay := time.Duration(math.Pow(2, float64(attempt))) * time.Second
	if delay > maxBackoff {
		delay = maxBackoff
	}

	return delay
}

func sleepCtx(ctx context.Context, d time.Duration) {
	timer := time.NewTimer(d)
	defer timer.Stop()

	select {
	case <-ctx.Done():
	case <-timer.C:
	}
}
