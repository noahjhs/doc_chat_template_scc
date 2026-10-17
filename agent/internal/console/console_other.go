//go:build !windows

package console

// AttachForCLI does nothing off Windows: a binary always has its terminal.
func AttachForCLI() {}
