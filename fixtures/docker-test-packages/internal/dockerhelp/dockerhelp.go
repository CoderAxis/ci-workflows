// Package dockerhelp is a shared test helper that reaches the daemon itself, so a test importing it
// needs Docker without naming any container library.
package dockerhelp

import "github.com/moby/moby/client"

func Up() bool { return client.DaemonReachable() }
