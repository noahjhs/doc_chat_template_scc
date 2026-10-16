package mirror

import "testing"

func TestDevicesToRekey(t *testing.T) {
	folder := func(id, pw string) stFolder {
		return stFolder{ID: id, Devices: []stFolderDevice{{DeviceID: "RILEY", EncryptionPassword: pw}, {DeviceID: "PLAIN"}}}
	}
	have := []stFolder{folder("casper-a", "pw-a")}
	got := devicesToRekey(map[string]stFolder{"casper-a": folder("casper-a", "pw-a")}, have)
	if len(got) != 0 {
		t.Fatalf("an unchanged folder must not reconnect anyone: %v", got)
	}
	got = devicesToRekey(map[string]stFolder{"casper-a": folder("casper-a", "pw-a"), "casper-b": folder("casper-b", "pw-b")}, have)
	if len(got) != 1 || !got["RILEY"] {
		t.Fatalf("a new encrypted folder must reconnect its mirror (only): %v", got)
	}
	got = devicesToRekey(map[string]stFolder{"casper-a": folder("casper-a", "pw-new")}, have)
	if !got["RILEY"] {
		t.Fatalf("a changed password must reconnect: %v", got)
	}
}
