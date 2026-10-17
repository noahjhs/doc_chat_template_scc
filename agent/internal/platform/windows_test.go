package platform

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

func TestListensInboundSetting(t *testing.T) {
	t.Setenv("CASPER_LISTEN_INBOUND", "1")
	if !ListensInbound() {
		t.Fatal("override on")
	}
	t.Setenv("CASPER_LISTEN_INBOUND", "0")
	if ListensInbound() {
		t.Fatal("override off")
	}
	t.Setenv("CASPER_LISTEN_INBOUND", "")
	if ListensInbound() != (runtime.GOOS != "windows") {
		t.Fatal("default: inbound everywhere but Windows")
	}
}

func TestOrdinaryFileIsNotCloudOnly(t *testing.T) {
	p := filepath.Join(t.TempDir(), "f")
	os.WriteFile(p, []byte("x"), 0o644)
	if IsCloudOnly(p) {
		t.Fatal("a local file is not a placeholder")
	}
}

// A child tied to Casper's lifetime is still an ordinary child that can be
// started, waited on and killed (the job-object assignment must not break it).
func TestKillOnExitLeavesTheChildWorking(t *testing.T) {
	name, args := "sleep", []string{"30"}
	if runtime.GOOS == "windows" {
		name, args = "ping", []string{"-n", "30", "127.0.0.1"}
	}
	cmd := exec.Command(name, args...)
	HideWindow(cmd)
	if err := cmd.Start(); err != nil {
		t.Skip(err)
	}
	if err := KillOnExit(cmd); err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() { cmd.Wait(); close(done) }()
	select {
	case <-done:
		t.Fatal("child ended by itself")
	case <-time.After(300 * time.Millisecond):
	}
	cmd.Process.Kill()
	<-done
}
