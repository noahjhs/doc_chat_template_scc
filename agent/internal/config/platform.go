package config

import "runtime"

// Platform is the operating system as casper_service names it ("macos",
// "windows"), sent when pairing and with every presence report.
func Platform() string {
	if runtime.GOOS == "darwin" {
		return "macos"
	}
	return runtime.GOOS
}
