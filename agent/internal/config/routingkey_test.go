package config

import "testing"

func TestLoadOrCreateRoutingKey_PersistsAndReuses(t *testing.T) {
	dir := t.TempDir()
	isolateHome(t, dir)

	first, err := LoadOrCreateRoutingKey()
	if err != nil {
		t.Fatalf("LoadOrCreateRoutingKey: %v", err)
	}
	if first == "" {
		t.Fatal("expected a non-empty routing key")
	}

	second, err := LoadOrCreateRoutingKey()
	if err != nil {
		t.Fatalf("LoadOrCreateRoutingKey (second call): %v", err)
	}
	if second != first {
		t.Fatalf("expected the same routing key across calls, got %q then %q", first, second)
	}
}

func TestLoadOrCreateRoutingKey_DistinctPerConfigDir(t *testing.T) {
	dirA, dirB := t.TempDir(), t.TempDir()

	isolateHome(t, dirA)
	keyA, err := LoadOrCreateRoutingKey()
	if err != nil {
		t.Fatalf("LoadOrCreateRoutingKey (A): %v", err)
	}

	isolateHome(t, dirB)
	keyB, err := LoadOrCreateRoutingKey()
	if err != nil {
		t.Fatalf("LoadOrCreateRoutingKey (B): %v", err)
	}

	if keyA == keyB {
		t.Fatal("expected distinct routing keys for distinct config dirs")
	}
}
