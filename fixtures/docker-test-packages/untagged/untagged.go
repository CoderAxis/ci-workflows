// Package untagged has a container test that is NOT behind the integration tag. Its test file is
// compiled only with fixture_untagged_docker, so the rest of the fixture stays a clean tree.
package untagged

func Value() int { return 1 }
