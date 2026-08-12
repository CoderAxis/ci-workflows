// Tests for the syntactic facts the policy layer rules on. The pair that matters
// most is the accessor inference and its negative: a field spelled `ID` must be
// recognised as the event id when its type declares EventID() returning it, and
// must be invisible when it does not. Without the second half this is the
// 613-finding version of the gate, which flags the id of every Product, Plan,
// Subscription and User in the fleet and gets switched off.
package main

import (
	"os"
	"path/filepath"
	"testing"
)

func testConfig() config {
	return config{
		Constructors: []constructorRule{
			{Pkg: "github.com/google/uuid", Func: "New", Kind: "random-v4"},
			{Pkg: "github.com/google/uuid", Func: "NewRandom", Kind: "random-v4"},
			{Pkg: "github.com/google/uuid", Func: "NewV7", Kind: "fresh-v7"},
			{Pkg: "github.com/google/uuid", Func: "NewSHA1", Kind: "deterministic-v5"},
		},
		Unwrappers: []string{"github.com/google/uuid.Must", "String"},
		Sinks: []sinkRule{
			{Field: "EventID", Types: []string{"outbox.Event", "Event"}},
			{Field: "IdempotencyKey", Types: []string{"outbox.Event", "Event"}},
		},
		SkipTestFiles: true,
	}
}

// scanFiles writes a one-module repository and returns the report for it. Paths
// are slash-separated and relative to the repository root.
func scanFiles(t *testing.T, files map[string]string) report {
	t.Helper()
	root := t.TempDir()
	if _, ok := files["go.mod"]; !ok {
		files["go.mod"] = "module example.com/fixture\n\ngo 1.22\n"
	}
	for rel, body := range files {
		path := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatalf("mkdir for %s: %v", rel, err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatalf("write %s: %v", rel, err)
		}
	}
	s, err := newScanner(testConfig(), root)
	if err != nil {
		t.Fatalf("newScanner: %v", err)
	}
	if err := s.run(); err != nil {
		t.Fatalf("run: %v", err)
	}
	if len(s.rep.ParseErrors) != 0 {
		t.Fatalf("fixture does not parse: %v", s.rep.ParseErrors)
	}
	return s.rep
}

func sinksFor(rep report, field string) []sinkFact {
	var out []sinkFact
	for _, s := range rep.Sinks {
		if s.Field == field {
			out = append(out, s)
		}
	}
	return out
}

// TestAccessorMakesAFieldTheEventID is identity-core's live shape: the field is
// spelled ID and only IdentityCreatedEvent.EventID() says it is the event id.
func TestAccessorMakesAFieldTheEventID(t *testing.T) {
	rep := scanFiles(t, map[string]string{"internal/events/events.go": `package events

import "github.com/google/uuid"

type IdentityCreatedEvent struct {
	ID         uuid.UUID
	IdentityID uuid.UUID
}

func (e IdentityCreatedEvent) EventID() uuid.UUID     { return e.ID }
func (e IdentityCreatedEvent) AggregateID() uuid.UUID { return e.IdentityID }

func Build() IdentityCreatedEvent {
	return IdentityCreatedEvent{ID: uuid.New(), IdentityID: uuid.New()}
}
`})

	got := sinksFor(rep, "ID")
	if len(got) != 1 {
		t.Fatalf("want exactly one sink fact for ID, got %d: %+v", len(got), rep.Sinks)
	}
	f := got[0]
	if f.SinkField != "EventID" {
		t.Errorf("ID must answer to the EventID sink, got sink_field %q", f.SinkField)
	}
	if f.ValueKind != "random-v4" {
		t.Errorf("uuid.New() into the event id must classify as random-v4, got %q", f.ValueKind)
	}
	if f.InferredFrom != "IdentityCreatedEvent.EventID()" {
		t.Errorf("the finding must name the accessor that established the sink, got %q", f.InferredFrom)
	}
	// AggregateID() exists on the same type and is not a configured sink, so the
	// field it returns must stay invisible: the inference is keyed on the sink
	// set, not on "any accessor".
	if n := len(sinksFor(rep, "IdentityID")); n != 0 {
		t.Errorf("IdentityID is returned by AggregateID(), which no sink names; got %d fact(s)", n)
	}
}

// TestPlainIDFieldIsNotASink is the negative that keeps the gate usable. A domain
// entity with an ID field and no accessor naming it an event id is not the
// policy's business, whatever version it is minted with.
func TestPlainIDFieldIsNotASink(t *testing.T) {
	rep := scanFiles(t, map[string]string{"internal/catalog/product.go": `package catalog

import "github.com/google/uuid"

type Product struct {
	ID    uuid.UUID
	OrgID uuid.UUID
	Name  string
}

// Slug is a method, and a getter, and returns a field - but it is not named
// after any sink, so it establishes nothing.
func (p Product) Slug() string { return p.Name }

func NewProduct(org uuid.UUID, name string) Product {
	return Product{ID: uuid.New(), OrgID: org, Name: name}
}
`})

	if len(rep.Sinks) != 0 {
		t.Fatalf("a plain ID field on a domain struct must produce no sink fact; got %+v", rep.Sinks)
	}
}

// TestAccessorInAnotherFileOfThePackage pins package-level scope. The struct, the
// accessor and the literal are routinely three different files.
func TestAccessorInAnotherFileOfThePackage(t *testing.T) {
	rep := scanFiles(t, map[string]string{
		"internal/events/types.go": `package events

import "github.com/google/uuid"

type OrderCreated struct {
	ID      uuid.UUID
	OrderID uuid.UUID
}
`,
		"internal/events/accessors.go": `package events

import "github.com/google/uuid"

func (e OrderCreated) EventID() uuid.UUID { return e.ID }
`,
		"internal/events/build.go": `package events

import "github.com/google/uuid"

func Build() OrderCreated { return OrderCreated{ID: uuid.New()} }
`,
	})

	got := sinksFor(rep, "ID")
	if len(got) != 1 || got[0].SinkField != "EventID" || got[0].ValueKind != "random-v4" {
		t.Fatalf("an accessor declared in another file of the same package must still bind the sink; got %+v", rep.Sinks)
	}
}

// TestAccessorInAnotherPackageOfTheModule is identity-core exactly: the literal
// is built in the application package and the type is declared in the core one.
func TestAccessorInAnotherPackageOfTheModule(t *testing.T) {
	rep := scanFiles(t, map[string]string{
		"identity.go": `package identitycore

import "github.com/google/uuid"

type IdentityCreatedEvent struct {
	ID         uuid.UUID
	IdentityID uuid.UUID
}

func (e IdentityCreatedEvent) EventID() uuid.UUID { return e.ID }
`,
		"application/creation/module.go": `package creation

import (
	"github.com/google/uuid"

	identitycore "example.com/fixture"
)

func Handle() identitycore.IdentityCreatedEvent {
	return identitycore.IdentityCreatedEvent{ID: uuid.New()}
}
`,
	})

	got := sinksFor(rep, "ID")
	if len(got) != 1 {
		t.Fatalf("want one sink fact across the package boundary, got %d: %+v", len(got), rep.Sinks)
	}
	if got[0].File != "application/creation/module.go" {
		t.Errorf("the finding belongs at the literal, not at the accessor; got %s", got[0].File)
	}
	if got[0].SinkField != "EventID" || got[0].ValueKind != "random-v4" {
		t.Errorf("want the EventID sink with a random-v4 value, got %+v", got[0])
	}
}

// TestAccessorRequiresAPlainGetter keeps the inference to the accessor shape. A
// method that takes an argument, or returns more than the id, or returns
// something other than one of its own fields, establishes nothing - each of those
// is a computation the scanner cannot follow without types.
func TestAccessorRequiresAPlainGetter(t *testing.T) {
	rep := scanFiles(t, map[string]string{"internal/events/events.go": `package events

import "github.com/google/uuid"

type WithArg struct{ ID uuid.UUID }

func (e WithArg) EventID(fallback uuid.UUID) uuid.UUID { return e.ID }

type WithTwoResults struct{ ID uuid.UUID }

func (e WithTwoResults) EventID() (uuid.UUID, error) { return e.ID, nil }

type Wrapping struct{ inner WithArg }

func (e Wrapping) EventID() uuid.UUID { return e.inner.ID }

func Build() (WithArg, WithTwoResults, Wrapping) {
	return WithArg{ID: uuid.New()}, WithTwoResults{ID: uuid.New()}, Wrapping{}
}
`})

	if len(rep.Sinks) != 0 {
		t.Fatalf("none of these is a plain accessor over a field of its own type; got %+v", rep.Sinks)
	}
}

// TestNamedSinkFieldStillCarriesItsOwnRole guards the ordinary path: a field
// matched by name answers to itself and is not marked as inferred, which is what
// the policy layer keys the spelled-type constraint on.
func TestNamedSinkFieldStillCarriesItsOwnRole(t *testing.T) {
	rep := scanFiles(t, map[string]string{"internal/emit/emit.go": `package emit

import (
	"github.com/coderaxis/platform-shared-go/outbox"
	"github.com/google/uuid"
)

func Emit() outbox.Event {
	return outbox.Event{EventID: uuid.New(), IdempotencyKey: uuid.Must(uuid.NewV7())}
}
`})

	if len(rep.Sinks) != 2 {
		t.Fatalf("want a fact for each named sink field, got %+v", rep.Sinks)
	}
	for _, f := range rep.Sinks {
		if f.SinkField != f.Field {
			t.Errorf("%s was matched by name and must answer to itself, got %q", f.Field, f.SinkField)
		}
		if f.InferredFrom != "" {
			t.Errorf("%s was matched by name and must not be marked inferred, got %q", f.Field, f.InferredFrom)
		}
		if f.LitType != "outbox.Event" {
			t.Errorf("%s must keep the spelled literal type for the type constraint, got %q", f.Field, f.LitType)
		}
	}
}

// TestSkipPathsLeavesTheCheckersOwnCheckoutUnwalked covers the other half of the
// scope fix: CI places this repository inside the tree under test, and its own
// violating fixture must not be read as the caller's code.
func TestSkipPathsLeavesTheCheckersOwnCheckoutUnwalked(t *testing.T) {
	root := t.TempDir()
	write := func(rel, body string) {
		path := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatalf("mkdir: %v", err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatalf("write: %v", err)
		}
	}
	write("go.mod", "module example.com/caller\n\ngo 1.22\n")
	write("emit/emit.go", `package emit

import (
	"github.com/coderaxis/platform-shared-go/outbox"
	"github.com/google/uuid"
)

func Emit() outbox.Event { return outbox.Event{EventID: uuid.Must(uuid.NewV7())} }
`)
	// The checkout the workflow makes, under a name that is NOT dot-prefixed so
	// the fact being tested is the path exclusion and not the dot-directory rule
	// that happens to cover it today.
	write("uuid-tools/fixtures/violating/emit.go", `package violating

import (
	"github.com/coderaxis/platform-shared-go/outbox"
	"github.com/google/uuid"
)

func Emit() outbox.Event { return outbox.Event{EventID: uuid.New()} }
`)

	cfg := testConfig()
	cfg.SkipPaths = []string{filepath.Join(root, "uuid-tools")}
	s, err := newScanner(cfg, root)
	if err != nil {
		t.Fatalf("newScanner: %v", err)
	}
	if err := s.run(); err != nil {
		t.Fatalf("run: %v", err)
	}
	for _, f := range s.rep.Sinks {
		if f.ValueKind == "random-v4" {
			t.Fatalf("the excluded checkout was scanned: %+v", f)
		}
	}
	if len(s.rep.Sinks) != 1 {
		t.Fatalf("the caller's own code must still be scanned, got %+v", s.rep.Sinks)
	}
}
