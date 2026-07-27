package rawsql

// Fixture for scripts/check_no_raw_sql.py. Every `wantSQL` line must be reported and every
// `wantProse` line must not be. The prose cases are the reason this fixture exists: the guard
// matched its opening keywords case-insensitively, so fmt.Errorf("update backup codes: %w", err)
// was reported as raw SQL against auth-core. A check that flags ordinary error strings gets
// ignored, and then it is not a check.

import "fmt"

// wantSQL
func selectStmt(db D) { db.Query("SELECT id, email FROM users WHERE id = $1", id) }

// wantSQL
func updateStmt(db D) { db.Exec("UPDATE users SET email = $1 WHERE id = $2", e, id) }

// wantSQL
func insertStmt(db D) { db.Exec("INSERT INTO users (id) VALUES ($1)", id) }

// wantSQL
func deleteStmt(db D) { db.Exec("DELETE FROM users WHERE id = $1", id) }

// wantSQL
func cteStmt(db D) { db.Query("WITH recent AS (SELECT id FROM users) SELECT * FROM recent") }

// wantSQL - lowercase SQL is still SQL
func lowerStmt(db D) { db.Exec("update users set email = $1 where id = $2", e, id) }

// wantProse - the auth-core false positive
func proseUpdate() error { return fmt.Errorf("update backup codes: %w", err) }

// wantProse
func proseWith() error { return fmt.Errorf("with the given credentials: %w", err) }

// wantProse
func proseSelect() error { return fmt.Errorf("select a factor before verifying") }

// wantProse
func proseDeleteWord() error { return fmt.Errorf("update failed, nothing to do") }
