// Command runner is the Customer-Side Runner. The connection lifecycle arrives in T-5.2.
package main

import (
	"log/slog"
	"os"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	logger.Error("runner is not implemented yet", "task", "T-5.2")
	os.Exit(1)
}
