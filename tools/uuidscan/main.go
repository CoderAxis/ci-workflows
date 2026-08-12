// Command uuidscan extracts, from Go source alone, the syntactic facts that a
// UUID-version policy decision needs. It decides nothing: it emits JSON and the
// policy layer (scripts/check-uuid-version-policy.py, driven by
// controls/uuid-policy.yaml) rules on it. Keeping the two apart is what lets the
// control catalogue stay the single source of truth while the parsing stays
// exact.
//
// Why a real parser instead of grep. The policy's own regression cases defeat a
// text scan: the FIXED voice-gateway emitter quotes both "uuid.NewSHA1" and
// "uuid.Must(uuid.NewV7())" in a doc comment explaining the bug it removed, so a
// grep-based gate reports the fix as the defect. go/parser puts comments in
// file.Comments and code in the AST, so a declaration marker can be read from
// prose while a constructor can only ever be read from syntax.
//
// Why syntax-only, and not go/analysis. A go/analysis pass needs type
// information, which needs the module's dependencies to resolve — private
// module auth, a warm module cache, and a build that succeeds, for all ~86
// consumer repos. parser.ParseFile needs a file. That is the difference between
// a gate that runs fleet-wide today from the CI repo and one that needs a Go
// module bump plus credentials in every repo. The cost is real and stated in the
// residue: without types, a sink is matched on field NAME (constrained by the
// composite literal's spelled type, or by the accessor below) and taint is
// followed through calls the parser can see, never through an interface or a
// func value.
//
// A sink field is therefore identified two ways. The configured name is one
// (`EventID`). The other is syntactic inference: a field IS the event id when
// its declaring type has an EventID() method that returns it, whatever the field
// is spelled. That is decidable from source, and it is the step that a
// name-only match got wrong — the two defects this gate missed both spell the
// field `ID`, on IdentityCreatedEvent and on domain.OrderCreated, so a
// name-only scan emitted no sink fact at all for identity-core and reported the
// live path clean. Adding `ID` as a sink name instead was measured at 613
// fleet-wide findings, nearly all entity ids on domain structs where v4 is
// nobody's violation; the accessor is what separates the two.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// ---------------------------------------------------------------------------
// Configuration (written by the Python policy layer from controls/uuid-policy.yaml)
// ---------------------------------------------------------------------------

type constructorRule struct {
	Pkg  string `json:"pkg"`  // import path, e.g. github.com/google/uuid
	Func string `json:"func"` // function name, e.g. NewSHA1
	Kind string `json:"kind"` // deterministic-v5 | deterministic-v3 | fresh-v7 | random-v4
}

type sinkRule struct {
	Field string   `json:"field"` // struct field name, e.g. EventID
	Types []string `json:"types"` // acceptable spelled literal types; empty = any
}

type config struct {
	Constructors []constructorRule `json:"constructors"`
	// Unwrappers are calls that pass their UUID-ness through, e.g. uuid.Must(x)
	// or x.String(). Keyed "pkg.Func" for calls, or bare method names.
	Unwrappers      []string   `json:"unwrappers"`
	Sinks           []sinkRule `json:"sinks"`
	MarkerPattern   string     `json:"marker_pattern"`
	DeterminismName string     `json:"determinism_name_pattern"`
	SkipDirs        []string   `json:"skip_dirs"`
	// SkipPaths are absolute directory paths to leave unwalked, used for the
	// checker's own checkout when CI has placed it inside the tree under test.
	// A path rather than a directory NAME because the name is the workflow's
	// choice and differs per job, while matching a bare name would also skip a
	// same-named directory that really belongs to the repository being scanned.
	SkipPaths     []string `json:"skip_paths"`
	SkipTestFiles bool     `json:"skip_test_files"`
}

// ---------------------------------------------------------------------------
// Emitted facts
// ---------------------------------------------------------------------------

type position struct {
	File string `json:"file"`
	Line int    `json:"line"`
}

// constructorFact is one syntactic call to a UUID constructor.
type constructorFact struct {
	position
	Kind     string `json:"kind"`
	Expr     string `json:"expr"`
	InFunc   string `json:"in_func"`
	Declared bool   `json:"declared"`   // a marker covers this site
	Marker   int    `json:"marker_idx"` // index into Markers, -1 when none
}

// sinkFact is one assignment into a field the policy cares about.
type sinkFact struct {
	position
	Field string `json:"field"`
	// SinkField is the configured sink this fact answers to. It equals Field
	// when the field was matched by name, and names the sink the accessor
	// established when it was not: `ID` on a type with an EventID() method that
	// returns it answers to the EventID sink.
	SinkField string `json:"sink_field"`
	// InferredFrom names the accessor that established the sink, e.g.
	// "IdentityCreatedEvent.EventID()". Empty when the field name was configured,
	// so the policy layer can tell evidence-by-inference from evidence-by-name
	// and apply the spelled-type constraint only to the latter.
	InferredFrom string `json:"inferred_from"`
	LitType      string `json:"lit_type"`
	ValueKind    string `json:"value_kind"`
	ValueExpr    string `json:"value_expr"`
	Via          string `json:"via"`  // same-repo function the value came through
	Site         string `json:"site"` // composite-literal | field-assign
	InFunc       string `json:"in_func"`
}

// funcFact is a function whose returned UUID kind, or whose honesty about
// determinism, the policy layer needs to rule on.
type funcFact struct {
	position
	Name string `json:"name"`
	// Recv is non-empty for methods. Interface-satisfying methods legitimately
	// ignore parameters, so the policy layer scores them differently.
	Recv string `json:"recv"`
	// Kind is the resolved kind this function returns: a single kind, "mixed"
	// when it returns both deterministic and nondeterministic UUIDs, or "".
	Kind string `json:"kind"`
	// ReturnKinds is every kind any return statement yields.
	ReturnKinds []string `json:"return_kinds"`
	Params      int      `json:"params"`
	// AllParamsUnused is true when the function declares parameters and
	// references none of them: it cannot be a function of its inputs.
	AllParamsUnused bool `json:"all_params_unused"`
	// NameClaimsDeterminism is true when the identifier promises the result is
	// derived from the inputs.
	NameClaimsDeterminism bool `json:"name_claims_determinism"`
}

type markerFact struct {
	position
	Version string `json:"version"`
	Reason  string `json:"reason"`
	ADR     string `json:"adr"`
	Raw     string `json:"raw"`
	// Covers counts constructor sites this marker declares. Zero means the
	// marker is orphaned: the code it excused is gone and the excuse is stale.
	Covers int `json:"covers"`
}

type report struct {
	Root         string            `json:"root"`
	Module       string            `json:"module"`
	Files        int               `json:"files_scanned"`
	Packages     int               `json:"packages_scanned"`
	ParseErrors  []string          `json:"parse_errors"`
	Constructors []constructorFact `json:"constructors"`
	Sinks        []sinkFact        `json:"sinks"`
	Funcs        []funcFact        `json:"funcs"`
	Markers      []markerFact      `json:"markers"`
}

// ---------------------------------------------------------------------------
// Scanner
// ---------------------------------------------------------------------------

const (
	kindUnknown = ""
	kindMixed   = "mixed"
)

// isDeterministic reports whether a kind names a value derived from its inputs.
func isDeterministic(kind string) bool {
	return strings.HasPrefix(kind, "deterministic-")
}

type scanner struct {
	cfg  config
	fset *token.FileSet
	root string
	mod  string

	markerRE      *regexp.Regexp
	determinismRE *regexp.Regexp

	// ctors maps "importpath.Func" -> kind.
	ctors map[string]string
	// unwrap is the set of pass-through calls, by "importpath.Func" and by bare
	// method name.
	unwrap map[string]bool
	// sinks maps field name -> acceptable spelled literal types (nil = any).
	sinks map[string][]string
	// accessors maps "dir\x00TypeName" -> field name -> the sink field whose
	// accessor method returns that field. It is what lets a field spelled `ID`
	// be recognised as the event id, and it is keyed by DIRECTORY rather than by
	// file because the method and the struct routinely sit in different files of
	// the same package (and the composite literal in a third package entirely).
	accessors map[string]map[string]string

	// funcKinds maps "dir\x00FuncName" -> resolved kind, filled to a fixpoint.
	funcKinds map[string]string
	// importDir maps an import path inside this module to its directory.
	importDir map[string]string
	// spans records each function's line range so a doc-comment marker can be
	// paired with a constructor inside that function's body.
	spans []funcSpan

	rep report
}

// pkgFile is one parsed file plus the per-file import alias table.
type pkgFile struct {
	dir     string
	path    string
	rel     string
	file    *ast.File
	aliases map[string]string // local name -> import path
}

func main() {
	var (
		rootFlag   = flag.String("root", ".", "repository root to scan")
		configFlag = flag.String("config", "", "path to the JSON config written by the policy layer")
		outFlag    = flag.String("out", "-", "where to write the JSON report ('-' is stdout)")
	)
	flag.Parse()

	if *configFlag == "" {
		fail("uuidscan: -config is required (the policy layer writes it from controls/uuid-policy.yaml)")
	}
	raw, err := os.ReadFile(*configFlag)
	if err != nil {
		fail("uuidscan: read config: %v", err)
	}
	var cfg config
	if err := json.Unmarshal(raw, &cfg); err != nil {
		fail("uuidscan: parse config: %v", err)
	}
	root, err := filepath.Abs(*rootFlag)
	if err != nil {
		fail("uuidscan: resolve root: %v", err)
	}

	s, err := newScanner(cfg, root)
	if err != nil {
		fail("uuidscan: %v", err)
	}
	if err := s.run(); err != nil {
		fail("uuidscan: %v", err)
	}

	out, err := json.MarshalIndent(s.rep, "", "  ")
	if err != nil {
		fail("uuidscan: encode report: %v", err)
	}
	out = append(out, '\n')
	if *outFlag == "-" {
		os.Stdout.Write(out)
		return
	}
	if err := os.WriteFile(*outFlag, out, 0o644); err != nil {
		fail("uuidscan: write report: %v", err)
	}
}

// fail exits 2. Exit 2 means "the scanner could not do its job", never "the
// repository is clean": a checker that cannot run must not look like a pass.
func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(2)
}

func newScanner(cfg config, root string) (*scanner, error) {
	markerPat := cfg.MarkerPattern
	if markerPat == "" {
		markerPat = `uuid:(?P<version>v[0-9])\s+reason=(?P<reason>[A-Za-z0-9._-]+)\s+adr=(?P<adr>[A-Za-z]+-[0-9]{4})`
	}
	markerRE, err := regexp.Compile(markerPat)
	if err != nil {
		return nil, fmt.Errorf("marker_pattern: %w", err)
	}
	detPat := cfg.DeterminismName
	if detPat == "" {
		detPat = `(?i)(deterministic|derive|stable)`
	}
	detRE, err := regexp.Compile(detPat)
	if err != nil {
		return nil, fmt.Errorf("determinism_name_pattern: %w", err)
	}

	s := &scanner{
		cfg:           cfg,
		fset:          token.NewFileSet(),
		root:          root,
		markerRE:      markerRE,
		determinismRE: detRE,
		ctors:         map[string]string{},
		unwrap:        map[string]bool{},
		sinks:         map[string][]string{},
		accessors:     map[string]map[string]string{},
		funcKinds:     map[string]string{},
		importDir:     map[string]string{},
	}
	for _, c := range cfg.Constructors {
		s.ctors[c.Pkg+"."+c.Func] = c.Kind
	}
	for _, u := range cfg.Unwrappers {
		s.unwrap[u] = true
	}
	for _, sk := range cfg.Sinks {
		s.sinks[sk.Field] = sk.Types
	}
	s.rep = report{Root: root, ParseErrors: []string{}, Constructors: []constructorFact{},
		Sinks: []sinkFact{}, Funcs: []funcFact{}, Markers: []markerFact{}}
	s.mod = readModulePath(root)
	s.rep.Module = s.mod
	return s, nil
}

// readModulePath returns the module path from go.mod, or "" when there is none.
// It is used only to resolve intra-repository imports back to directories, so a
// missing go.mod degrades recall (cross-package taint) and never correctness.
func readModulePath(root string) string {
	b, err := os.ReadFile(filepath.Join(root, "go.mod"))
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if rest, ok := strings.CutPrefix(line, "module "); ok {
			return strings.TrimSpace(rest)
		}
	}
	return ""
}

func (s *scanner) skipDir(name string) bool {
	for _, d := range s.cfg.SkipDirs {
		if name == d {
			return true
		}
	}
	return strings.HasPrefix(name, ".") && name != "." && name != ".."
}

// skipPath reports whether a directory is one the policy layer named outright,
// which today means the checker's own checkout inside the tree under test.
func (s *scanner) skipPath(path string) bool {
	clean := filepath.Clean(path)
	for _, p := range s.cfg.SkipPaths {
		if p == "" {
			continue
		}
		if clean == filepath.Clean(p) {
			return true
		}
	}
	return false
}

func (s *scanner) run() error {
	files, err := s.parseAll()
	if err != nil {
		return err
	}
	byDir := map[string][]*pkgFile{}
	for _, f := range files {
		byDir[f.dir] = append(byDir[f.dir], f)
	}
	s.rep.Files = len(files)
	s.rep.Packages = len(byDir)

	// Directories are reachable by import path, so a call into another package
	// of the same repository can be resolved to the function that was scanned.
	for dir := range byDir {
		rel, err := filepath.Rel(s.root, dir)
		if err != nil {
			continue
		}
		if s.mod == "" {
			continue
		}
		ip := s.mod
		if rel != "." {
			ip = s.mod + "/" + filepath.ToSlash(rel)
		}
		s.importDir[ip] = dir
	}

	// The accessor index has to exist before any sink is recorded, and it is
	// built from every file at once: the EventID() method and the struct it
	// belongs to are commonly in different files of one package.
	s.indexAccessors(files)

	// Resolving a function's kind can depend on another function's kind, so
	// iterate to a fixpoint. The bound is small because these chains are short
	// (the deepest real one in the fleet is two hops:
	// deterministicCallUUID -> mustUUIDv7 -> uuid.NewV7).
	for round := 0; round < 8; round++ {
		changed := false
		for _, f := range files {
			for _, d := range f.file.Decls {
				fd, ok := d.(*ast.FuncDecl)
				if !ok || fd.Body == nil {
					continue
				}
				key := f.dir + "\x00" + fd.Name.Name
				kind := s.resolveFuncKind(f, fd)
				if s.funcKinds[key] != kind {
					s.funcKinds[key] = kind
					changed = true
				}
			}
		}
		if !changed {
			break
		}
	}

	// With every function's kind settled, collect the reportable facts.
	for _, f := range files {
		s.collectMarkers(f)
	}
	for _, f := range files {
		s.collectFacts(f)
	}
	s.attachMarkers()
	return nil
}

func (s *scanner) parseAll() ([]*pkgFile, error) {
	var out []*pkgFile
	err := filepath.WalkDir(s.root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil // an unreadable subtree must not silence the whole scan
		}
		if d.IsDir() {
			if path != s.root && (s.skipDir(d.Name()) || s.skipPath(path)) {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") {
			return nil
		}
		base := d.Name()
		if s.cfg.SkipTestFiles && strings.HasSuffix(base, "_test.go") {
			return nil
		}
		if strings.HasSuffix(base, ".pb.go") || strings.HasSuffix(base, ".sql.go") {
			return nil // generated
		}
		af, perr := parser.ParseFile(s.fset, path, nil, parser.ParseComments|parser.SkipObjectResolution)
		if perr != nil {
			rel, _ := filepath.Rel(s.root, path)
			s.rep.ParseErrors = append(s.rep.ParseErrors, fmt.Sprintf("%s: %v", rel, perr))
			return nil
		}
		rel, _ := filepath.Rel(s.root, path)
		out = append(out, &pkgFile{
			dir: filepath.Dir(path), path: path, rel: filepath.ToSlash(rel),
			file: af, aliases: importAliases(af),
		})
		return nil
	})
	return out, err
}

// importAliases maps the name a file uses for each import to its import path,
// honouring explicit aliases and dot-free default names.
func importAliases(f *ast.File) map[string]string {
	m := map[string]string{}
	for _, im := range f.Imports {
		p := strings.Trim(im.Path.Value, `"`)
		name := p
		if i := strings.LastIndex(p, "/"); i >= 0 {
			name = p[i+1:]
		}
		if im.Name != nil {
			name = im.Name.Name
		}
		m[name] = p
	}
	return m
}

// ---------------------------------------------------------------------------
// Expression classification
// ---------------------------------------------------------------------------

// classify returns the UUID kind an expression yields, following pass-through
// calls, same-repository function calls and local variables. locals may be nil.
func (s *scanner) classify(f *pkgFile, expr ast.Expr, locals map[string]string, depth int) (kind string, via string) {
	if expr == nil || depth > 12 {
		return kindUnknown, ""
	}
	switch e := expr.(type) {
	case *ast.CallExpr:
		return s.classifyCall(f, e, locals, depth)
	case *ast.Ident:
		if locals != nil {
			if k, ok := locals[e.Name]; ok {
				return k, ""
			}
		}
		// A package-level var initialised from a constructor is reached through
		// the same local table, which collectFacts seeds per file.
		return kindUnknown, ""
	case *ast.ParenExpr:
		return s.classify(f, e.X, locals, depth+1)
	case *ast.SelectorExpr:
		// e.g. someStruct.field — not decidable without types.
		return kindUnknown, ""
	}
	return kindUnknown, ""
}

func (s *scanner) classifyCall(f *pkgFile, call *ast.CallExpr, locals map[string]string, depth int) (string, string) {
	switch fn := call.Fun.(type) {
	case *ast.SelectorExpr:
		// pkg.Func(...)
		if x, ok := fn.X.(*ast.Ident); ok {
			if ip, ok := f.aliases[x.Name]; ok {
				key := ip + "." + fn.Sel.Name
				if k, ok := s.ctors[key]; ok {
					return k, ""
				}
				if s.unwrap[key] && len(call.Args) > 0 {
					return s.classify(f, call.Args[0], locals, depth+1)
				}
				// A call into another package of this same repository.
				if dir, ok := s.importDir[ip]; ok {
					if k, ok := s.funcKinds[dir+"\x00"+fn.Sel.Name]; ok && k != kindUnknown {
						return k, fn.Sel.Name
					}
				}
				return kindUnknown, ""
			}
		}
		// value.String() / value.Bytes(): UUID-ness passes through the receiver.
		if s.unwrap[fn.Sel.Name] {
			return s.classify(f, fn.X, locals, depth+1)
		}
		return kindUnknown, ""
	case *ast.Ident:
		// Same-package call.
		if k, ok := s.funcKinds[f.dir+"\x00"+fn.Name]; ok && k != kindUnknown {
			return k, fn.Name
		}
		return kindUnknown, ""
	}
	return kindUnknown, ""
}

// resolveFuncKind reduces every return statement of fd to a single kind:
// the one kind it returns, "mixed" when it returns both a deterministic and a
// nondeterministic UUID, or "" when no return yields a classifiable UUID.
func (s *scanner) resolveFuncKind(f *pkgFile, fd *ast.FuncDecl) string {
	kinds := s.returnKinds(f, fd)
	return reduceKinds(kinds)
}

func reduceKinds(kinds []string) string {
	seen := map[string]bool{}
	for _, k := range kinds {
		if k != kindUnknown {
			seen[k] = true
		}
	}
	switch len(seen) {
	case 0:
		return kindUnknown
	case 1:
		for k := range seen {
			return k
		}
	}
	// Both a derived value and a fresh one can come out of here. That is not a
	// defect on its own (a deterministic deriver may fall back to a fresh id on
	// empty input) so it is reported as-is and judged by the policy layer.
	return kindMixed
}

func (s *scanner) returnKinds(f *pkgFile, fd *ast.FuncDecl) []string {
	locals := s.localKinds(f, fd)
	var kinds []string
	ast.Inspect(fd.Body, func(n ast.Node) bool {
		// Do not descend into a nested function literal: its returns are its
		// own, not this function's.
		if _, ok := n.(*ast.FuncLit); ok {
			return false
		}
		ret, ok := n.(*ast.ReturnStmt)
		if !ok {
			return true
		}
		for _, r := range ret.Results {
			if k, _ := s.classify(f, r, locals, 0); k != kindUnknown {
				kinds = append(kinds, k)
			}
		}
		return true
	})
	return kinds
}

// localKinds is a flow-insensitive, name-keyed taint table for one function
// body: every `x := <uuid expr>` or `x = <uuid expr>` records x's kind. Being
// flow-insensitive means a variable reassigned to a different kind resolves to
// the last binding seen in source order; that is a stated limit, not a
// soundness claim.
func (s *scanner) localKinds(f *pkgFile, fd *ast.FuncDecl) map[string]string {
	locals := map[string]string{}
	if fd.Body == nil {
		return locals
	}
	// Two rounds so `a := ctor(); b := a` resolves regardless of nesting order.
	for round := 0; round < 2; round++ {
		ast.Inspect(fd.Body, func(n ast.Node) bool {
			switch st := n.(type) {
			case *ast.AssignStmt:
				for i, lhs := range st.Lhs {
					id, ok := lhs.(*ast.Ident)
					if !ok || id.Name == "_" || i >= len(st.Rhs) {
						continue
					}
					if k, _ := s.classify(f, st.Rhs[i], locals, 0); k != kindUnknown {
						locals[id.Name] = k
					}
				}
			case *ast.ValueSpec:
				for i, name := range st.Names {
					if i >= len(st.Values) {
						continue
					}
					if k, _ := s.classify(f, st.Values[i], locals, 0); k != kindUnknown {
						locals[name.Name] = k
					}
				}
			}
			return true
		})
	}
	return locals
}

// fileLevelKinds records package-level `var x = <uuid expr>` bindings so a sink
// fed from a package var is still classified.
func (s *scanner) fileLevelKinds(f *pkgFile) map[string]string {
	out := map[string]string{}
	for _, d := range f.file.Decls {
		gd, ok := d.(*ast.GenDecl)
		if !ok || (gd.Tok != token.VAR && gd.Tok != token.CONST) {
			continue
		}
		for _, spec := range gd.Specs {
			vs, ok := spec.(*ast.ValueSpec)
			if !ok {
				continue
			}
			for i, name := range vs.Names {
				if i >= len(vs.Values) {
					continue
				}
				if k, _ := s.classify(f, vs.Values[i], nil, 0); k != kindUnknown {
					out[name.Name] = k
				}
			}
		}
	}
	return out
}

// ---------------------------------------------------------------------------
// Sink identity by accessor
// ---------------------------------------------------------------------------

// indexAccessors records, for every configured sink field F, each type that
// declares a method named F returning one of its own fields. That field then IS
// the sink, whatever it is spelled.
//
// The inference is the whole of what the field-NAME match was missing, and it is
// deliberately narrow. It fires only on a method that takes no argument, returns
// exactly one value, and returns a direct `recv.Field` selector — the shape of
// the Event interface's accessor (EventID() uuid.UUID) and of nothing else. A
// type that merely has an `ID` field is not touched, which is the difference
// between this and configuring `ID` as a sink name: the latter was measured at
// 613 fleet-wide findings on Product, Plan, Subscription and User ids that no
// document versions, and that volume is what gets a gate switched off.
func (s *scanner) indexAccessors(files []*pkgFile) {
	for _, f := range files {
		for _, d := range f.file.Decls {
			fd, ok := d.(*ast.FuncDecl)
			if !ok || fd.Body == nil || fd.Recv == nil || len(fd.Recv.List) == 0 {
				continue
			}
			if _, watched := s.sinks[fd.Name.Name]; !watched {
				continue
			}
			if fd.Type.Params != nil && len(fd.Type.Params.List) > 0 {
				continue // takes an argument, so it is not a plain accessor
			}
			if fd.Type.Results == nil || len(fd.Type.Results.List) != 1 {
				continue // returns nothing, or more than the id
			}
			recvName, typeName := receiverParts(fd.Recv.List[0])
			if recvName == "" || typeName == "" {
				continue
			}
			key := f.dir + "\x00" + typeName
			for _, field := range returnedFields(fd, recvName) {
				if field == fd.Name.Name {
					continue // already matched by name; nothing to infer
				}
				if s.accessors[key] == nil {
					s.accessors[key] = map[string]string{}
				}
				s.accessors[key][field] = fd.Name.Name
			}
		}
	}
}

// receiverParts returns the receiver's variable name and the base name of its
// type, unwrapping a pointer receiver and type parameters. An unnamed receiver
// yields "", because a method that cannot name itself cannot return one of its
// own fields.
func receiverParts(recv *ast.Field) (name string, typeName string) {
	if len(recv.Names) > 0 && recv.Names[0].Name != "_" {
		name = recv.Names[0].Name
	}
	typeName = baseTypeName(recv.Type)
	return name, typeName
}

// baseTypeName reduces *Foo, Foo[T] and *Foo[T] to Foo, and yields "" for
// anything else.
func baseTypeName(e ast.Expr) string {
	switch x := e.(type) {
	case *ast.Ident:
		return x.Name
	case *ast.StarExpr:
		return baseTypeName(x.X)
	case *ast.IndexExpr:
		return baseTypeName(x.X)
	case *ast.IndexListExpr:
		return baseTypeName(x.X)
	}
	return ""
}

// returnedFields lists the receiver's own fields the accessor can return. All of
// them, not one: a method with a branch returning either field means both fields
// can be the id, and dropping the ambiguous case would lose exactly the recall
// this inference exists for.
func returnedFields(fd *ast.FuncDecl, recvName string) []string {
	seen := map[string]bool{}
	var out []string
	ast.Inspect(fd.Body, func(n ast.Node) bool {
		if _, ok := n.(*ast.FuncLit); ok {
			return false // a nested literal's returns are its own
		}
		ret, ok := n.(*ast.ReturnStmt)
		if !ok {
			return true
		}
		for _, r := range ret.Results {
			sel, ok := r.(*ast.SelectorExpr)
			if !ok {
				continue
			}
			id, ok := sel.X.(*ast.Ident)
			if !ok || id.Name != recvName {
				continue // e.g. e.wrapped.ID: not a field of this type
			}
			if !seen[sel.Sel.Name] {
				seen[sel.Sel.Name] = true
				out = append(out, sel.Sel.Name)
			}
		}
		return true
	})
	return out
}

// sinkRole reports which configured sink a composite-literal field answers to.
// A configured field name answers to itself; any other field answers to the sink
// its declaring type's accessor establishes.
func (s *scanner) sinkRole(f *pkgFile, litType ast.Expr, field string) (sinkField string, inferredFrom string, ok bool) {
	if _, watched := s.sinks[field]; watched {
		return field, "", true
	}
	dir, typeName, resolved := s.resolveLitType(f, litType)
	if !resolved {
		return "", "", false
	}
	if sink, found := s.accessors[dir+"\x00"+typeName][field]; found {
		return sink, typeName + "." + sink + "()", true
	}
	return "", "", false
}

// resolveLitType maps a composite literal's spelled type to the package
// directory that declares it. A bare name is this package; a qualified name is
// resolved through the file's import aliases and the intra-module import map, so
// identitycore.IdentityCreatedEvent written in package `creation` finds the
// accessor declared over in identity.go. A type from another MODULE resolves to
// nothing, which is a stated limit: order-core-postgres spells
// domain.OrderCreated but the type lives in the order-core repository, so the
// inference only fires when order-core itself is scanned.
func (s *scanner) resolveLitType(f *pkgFile, t ast.Expr) (dir string, typeName string, ok bool) {
	switch x := t.(type) {
	case nil:
		return "", "", false
	case *ast.Ident:
		return f.dir, x.Name, true
	case *ast.StarExpr:
		return s.resolveLitType(f, x.X)
	case *ast.IndexExpr:
		return s.resolveLitType(f, x.X)
	case *ast.IndexListExpr:
		return s.resolveLitType(f, x.X)
	case *ast.SelectorExpr:
		pkg, ok := x.X.(*ast.Ident)
		if !ok {
			return "", "", false
		}
		ip, ok := f.aliases[pkg.Name]
		if !ok {
			return "", "", false
		}
		d, ok := s.importDir[ip]
		if !ok {
			return "", "", false
		}
		return d, x.Sel.Name, true
	}
	return "", "", false
}

// ---------------------------------------------------------------------------
// Fact collection
// ---------------------------------------------------------------------------

func (s *scanner) collectFacts(f *pkgFile) {
	pkgLocals := s.fileLevelKinds(f)

	// Package-level constructor calls, e.g. `var ns = uuid.NewSHA1(...)`.
	for _, d := range f.file.Decls {
		if gd, ok := d.(*ast.GenDecl); ok {
			ast.Inspect(gd, func(n ast.Node) bool {
				if call, ok := n.(*ast.CallExpr); ok {
					s.recordConstructor(f, call, nil, "")
				}
				if cl, ok := n.(*ast.CompositeLit); ok {
					s.recordCompositeSinks(f, cl, pkgLocals, "")
				}
				return true
			})
		}
	}

	for _, d := range f.file.Decls {
		fd, ok := d.(*ast.FuncDecl)
		if !ok || fd.Body == nil {
			continue
		}
		locals := merge(pkgLocals, s.localKinds(f, fd))
		fname := funcName(fd)
		s.recordFunc(f, fd)

		ast.Inspect(fd.Body, func(n ast.Node) bool {
			switch node := n.(type) {
			case *ast.CallExpr:
				s.recordConstructor(f, node, locals, fname)
			case *ast.CompositeLit:
				s.recordCompositeSinks(f, node, locals, fname)
			case *ast.AssignStmt:
				// x.EventID = <expr>. Matched by configured name only: the
				// accessor inference needs the assignee's type, and `x` is a
				// variable whose type only a type checker knows. The composite
				// literal — which is the form both missed defects took — spells
				// its type, so that is where the inference can reach.
				for i, lhs := range node.Lhs {
					sel, ok := lhs.(*ast.SelectorExpr)
					if !ok || i >= len(node.Rhs) {
						continue
					}
					if _, watched := s.sinks[sel.Sel.Name]; !watched {
						continue
					}
					kind, via := s.classify(f, node.Rhs[i], locals, 0)
					s.rep.Sinks = append(s.rep.Sinks, sinkFact{
						position: s.at(f, sel.Sel.Pos()), Field: sel.Sel.Name,
						SinkField: sel.Sel.Name, LitType: "", ValueKind: kind, Via: via,
						ValueExpr: exprString(node.Rhs[i]), Site: "field-assign", InFunc: fname,
					})
				}
			}
			return true
		})
	}
}

func (s *scanner) at(f *pkgFile, p token.Pos) position {
	return position{File: f.rel, Line: s.fset.Position(p).Line}
}

// recordConstructor records only a DIRECT call to a configured constructor, so
// the constructor inventory counts real minting sites once, not every call that
// happens to carry a minted value.
func (s *scanner) recordConstructor(f *pkgFile, call *ast.CallExpr, locals map[string]string, inFunc string) {
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok {
		return
	}
	x, ok := sel.X.(*ast.Ident)
	if !ok {
		return
	}
	ip, ok := f.aliases[x.Name]
	if !ok {
		return
	}
	kind, ok := s.ctors[ip+"."+sel.Sel.Name]
	if !ok {
		return
	}
	s.rep.Constructors = append(s.rep.Constructors, constructorFact{
		position: s.at(f, call.Pos()), Kind: kind, Expr: exprString(call),
		InFunc: inFunc, Marker: -1,
	})
}

func (s *scanner) recordCompositeSinks(f *pkgFile, cl *ast.CompositeLit, locals map[string]string, inFunc string) {
	litType := exprString(cl.Type)
	for _, elt := range cl.Elts {
		kv, ok := elt.(*ast.KeyValueExpr)
		if !ok {
			continue
		}
		key, ok := kv.Key.(*ast.Ident)
		if !ok {
			continue
		}
		sinkField, inferredFrom, watched := s.sinkRole(f, cl.Type, key.Name)
		if !watched {
			continue
		}
		kind, via := s.classify(f, kv.Value, locals, 0)
		s.rep.Sinks = append(s.rep.Sinks, sinkFact{
			position: s.at(f, kv.Pos()), Field: key.Name, SinkField: sinkField,
			InferredFrom: inferredFrom, LitType: litType,
			ValueKind: kind, ValueExpr: exprString(kv.Value), Via: via,
			Site: "composite-literal", InFunc: inFunc,
		})
	}
}

func (s *scanner) recordFunc(f *pkgFile, fd *ast.FuncDecl) {
	kinds := s.returnKinds(f, fd)
	kind := reduceKinds(kinds)
	if kind == kindUnknown {
		return // returns no UUID this scanner can see; nothing to rule on
	}
	params, unusedAll := paramUsage(fd)
	recv := ""
	if fd.Recv != nil && len(fd.Recv.List) > 0 {
		recv = exprString(fd.Recv.List[0].Type)
	}
	seen := map[string]bool{}
	var uniq []string
	for _, k := range kinds {
		if !seen[k] {
			seen[k] = true
			uniq = append(uniq, k)
		}
	}
	s.rep.Funcs = append(s.rep.Funcs, funcFact{
		position: s.at(f, fd.Pos()), Name: fd.Name.Name, Recv: recv, Kind: kind,
		ReturnKinds: uniq, Params: params, AllParamsUnused: unusedAll,
		NameClaimsDeterminism: s.determinismRE.MatchString(fd.Name.Name),
	})
}

// paramUsage returns the declared parameter count and whether the body
// references none of them. A function that declares inputs and reads none
// cannot be a function of those inputs, whatever its name promises.
func paramUsage(fd *ast.FuncDecl) (int, bool) {
	if fd.Type.Params == nil {
		return 0, false
	}
	var names []string
	count := 0
	for _, field := range fd.Type.Params.List {
		if len(field.Names) == 0 {
			count++ // unnamed parameter: declared, unreadable, hence unused
			continue
		}
		for _, n := range field.Names {
			count++
			if n.Name != "_" {
				names = append(names, n.Name)
			}
		}
	}
	if count == 0 {
		return 0, false
	}
	if len(names) == 0 {
		return count, true // every parameter is _ or unnamed
	}
	used := map[string]bool{}
	if fd.Body != nil {
		ast.Inspect(fd.Body, func(n ast.Node) bool {
			if id, ok := n.(*ast.Ident); ok {
				used[id.Name] = true
			}
			return true
		})
	}
	for _, n := range names {
		if used[n] {
			return count, false
		}
	}
	return count, true
}

// ---------------------------------------------------------------------------
// Declaration markers
// ---------------------------------------------------------------------------

func (s *scanner) collectMarkers(f *pkgFile) {
	for _, cg := range f.file.Comments {
		for _, c := range cg.List {
			m := s.markerRE.FindStringSubmatch(c.Text)
			if m == nil {
				continue
			}
			get := func(name string) string {
				for i, n := range s.markerRE.SubexpNames() {
					if n == name && i < len(m) {
						return m[i]
					}
				}
				return ""
			}
			s.rep.Markers = append(s.rep.Markers, markerFact{
				position: s.at(f, c.Pos()), Version: get("version"),
				Reason: get("reason"), ADR: get("adr"), Raw: strings.TrimSpace(c.Text),
			})
		}
	}
	// Record the line span of every function so a marker in a doc comment can
	// cover a constructor further down the body.
	for _, d := range f.file.Decls {
		fd, ok := d.(*ast.FuncDecl)
		if !ok {
			continue
		}
		start := s.fset.Position(fd.Pos()).Line
		end := s.fset.Position(fd.End()).Line
		docStart := start
		if fd.Doc != nil {
			docStart = s.fset.Position(fd.Doc.Pos()).Line
		}
		s.spans = append(s.spans, funcSpan{file: f.rel, docStart: docStart, start: start, end: end})
	}
}

type funcSpan struct {
	file     string
	docStart int
	start    int
	end      int
}

// attachMarkers pairs each declaration marker with the constructor sites it
// excuses. A marker declares a constructor when it sits on the same line, on
// the line above, or in the doc comment of the function that contains it. The
// pairing is what makes staleness decidable: a marker that covers nothing is
// reported, so an excuse cannot outlive the code it excused.
//
// Only a DETERMINISTIC constructor can be a marker's subject. A marker declares
// an exception to "mint v7", so a fresh-v7 call adjacent to a //uuid:v5 marker
// does not satisfy it — that is precisely the shape of a marker left behind when
// the derivation it excused was replaced by a generator, which is the stale case
// the control exists to catch.
func (s *scanner) attachMarkers() {
	for ci := range s.rep.Constructors {
		c := &s.rep.Constructors[ci]
		if !isDeterministic(c.Kind) {
			continue
		}
		for mi := range s.rep.Markers {
			m := &s.rep.Markers[mi]
			if m.File != c.File {
				continue
			}
			if m.Line == c.Line || m.Line == c.Line-1 || s.sameFuncDoc(m, c) {
				c.Declared = true
				c.Marker = mi
				m.Covers++
				break
			}
		}
	}
}

func (s *scanner) sameFuncDoc(m *markerFact, c *constructorFact) bool {
	for _, sp := range s.spans {
		if sp.file != c.File {
			continue
		}
		if c.Line < sp.start || c.Line > sp.end {
			continue
		}
		if m.Line >= sp.docStart && m.Line <= sp.end {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func merge(a, b map[string]string) map[string]string {
	out := make(map[string]string, len(a)+len(b))
	for k, v := range a {
		out[k] = v
	}
	for k, v := range b {
		out[k] = v
	}
	return out
}

func funcName(fd *ast.FuncDecl) string {
	if fd.Recv != nil && len(fd.Recv.List) > 0 {
		return exprString(fd.Recv.List[0].Type) + "." + fd.Name.Name
	}
	return fd.Name.Name
}

// exprString renders an expression compactly for diagnostics. It is deliberately
// not go/printer: the output is read by humans in a CI annotation, so a short
// stable rendering beats a faithful one.
func exprString(e ast.Expr) string {
	switch x := e.(type) {
	case nil:
		return ""
	case *ast.Ident:
		return x.Name
	case *ast.SelectorExpr:
		return exprString(x.X) + "." + x.Sel.Name
	case *ast.CallExpr:
		var args []string
		for _, a := range x.Args {
			args = append(args, exprString(a))
		}
		return exprString(x.Fun) + "(" + strings.Join(args, ", ") + ")"
	case *ast.BasicLit:
		return x.Value
	case *ast.StarExpr:
		return "*" + exprString(x.X)
	case *ast.ArrayType:
		return "[]" + exprString(x.Elt)
	case *ast.BinaryExpr:
		return exprString(x.X) + x.Op.String() + exprString(x.Y)
	case *ast.ParenExpr:
		return "(" + exprString(x.X) + ")"
	case *ast.CompositeLit:
		return exprString(x.Type) + "{...}"
	case *ast.UnaryExpr:
		return x.Op.String() + exprString(x.X)
	case *ast.IndexExpr:
		return exprString(x.X) + "[" + exprString(x.Index) + "]"
	}
	return "expr"
}
