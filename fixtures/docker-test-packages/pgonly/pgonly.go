// Package pgonly is the *-core-postgres shape: integration-tagged tests that need a database and
// no Docker, so they stay in Build and test on the pool.
package pgonly

func Tagged() string { return "tagged" }
