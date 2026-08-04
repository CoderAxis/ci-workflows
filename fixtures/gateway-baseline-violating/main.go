package main

import (
	"io"
	"net"
	"time"

	"google.golang.org/grpc"
)

func proxyWebSocket(clientConn, backendConn net.Conn, backendHost string) {
	// net.DialTimeout("tcp", documentedOldBackend, time.Second) must not count.
	backendConn, _ = net.DialTimeout("tcp", backendHost, 10*time.Second)
	go io.Copy(backendConn, clientConn)
	go io.Copy(clientConn, backendConn)
}

func main() {
	_ = grpc.NewServer()
}
