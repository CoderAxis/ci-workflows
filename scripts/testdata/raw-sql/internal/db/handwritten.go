package db

// Hand-written code sharing a directory with generated code. The exemption has to come from the
// marker this file does not carry, not from the directory it sits in — otherwise pointing sqlc at
// internal/db would silently exempt every hand-written query anyone added beside it.

// wantSQL
func lookup(db D) { db.Query("SELECT id, email FROM identities WHERE email = $1", email) }
