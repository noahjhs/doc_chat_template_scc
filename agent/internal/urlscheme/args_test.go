package urlscheme

import (
	"reflect"
	"testing"
)

func TestFromArgs(t *testing.T) {
	got := FromArgs([]string{"-x", "casper-dev://pair?token=a&username=b", "CASPER-DEV://pair?x=1", "casper://pair?y=2", "setup"}, "casper-dev")
	want := []string{"casper-dev://pair?token=a&username=b", "CASPER-DEV://pair?x=1"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	if len(FromArgs(nil, "casper")) != 0 {
		t.Fatal("no args, no links")
	}
}
