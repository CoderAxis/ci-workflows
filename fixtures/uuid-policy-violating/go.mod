// Fixture only. Never built; uuidscan parses it with go/parser, so the imports
// need not resolve. The module line exists so intra-repository import paths
// resolve back to directories and cross-package taint is exercised.
module example.com/uuid-policy-violating

go 1.22
