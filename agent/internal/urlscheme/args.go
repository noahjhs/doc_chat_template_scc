package urlscheme

import "strings"

// FromArgs picks the casper:// links out of a command line. Windows starts
// Casper with the link as an argument ("Casper.exe" "casper://pair?...").
func FromArgs(args []string, scheme string) []string {
	var urls []string
	prefix := strings.ToLower(scheme) + "://"
	for _, a := range args {
		if strings.HasPrefix(strings.ToLower(a), prefix) {
			urls = append(urls, a)
		}
	}
	return urls
}
