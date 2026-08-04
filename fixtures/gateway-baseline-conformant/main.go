package main

import (
	"time"

	"github.com/coderaxis/platform-shared-go/platform/gatewaybaseline"
	"github.com/coderaxis/platform-shared-go/platform/grpcx"
)

const maxConcurrentConnections = 100

func main() {
	_ = gatewaybaseline.Load()
	_, _ = grpcx.NewServer(grpcx.ServerConfig{})
	_ = 60 * time.Second // idle timeout is supplied to the terminating protocol server.
}
