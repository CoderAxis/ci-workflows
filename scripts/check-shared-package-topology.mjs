#!/usr/bin/env node
/**
 * Prove that the declared topology of the shared frontend packages, each app's
 * Dockerfile, and the working tree all agree.
 *
 * Run from anywhere:  node scripts/check-shared-package-topology.mjs
 * Exit code 0 = agreement, 1 = drift, 2 = the check itself could not run.
 *
 * WHAT THIS PREVENTS
 * A shared package's source of truth used to be implied by five mechanisms that
 * could each disagree: the pnpm workspace glob, the node_modules symlink, the
 * lockfile link: entry, the tsconfig paths mapping, and the Dockerfile clone.
 * Nothing checked that they matched, and twice they did not:
 *
 *   - web-design was maintained in two places for three months. The copy in
 *     frontend-core was consumed by no app and no image, drifted 76 files
 *     behind, and still received hand-mirrored edits.
 *   - enterprise-contracts had pnpm resolving core's package while TypeScript
 *     compiled a separate local repo under the same import specifier. Nothing
 *     broke only because the two held identical sources; the first edit to
 *     either would have split them silently.
 *
 * Both are the same failure: a second copy of a package that looks maintained.
 * This check makes that state unrepresentable rather than merely discouraged.
 *
 * WHY IT LIVES AT THE REPO ROOT
 * It has to see across repositories -- the apps, frontend-core, and the
 * standalone package repos are separate git repos, and the drift is BETWEEN
 * them. No single app's CI can perform this check, because an app's Docker build
 * context is that app alone and its dependencies are cloned from GitHub. So this
 * belongs to the combined working tree, alongside the other cross-repo scripts.
 */

import { execFileSync } from 'node:child_process';
import { existsSync, lstatSync, readFileSync, readdirSync, readlinkSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// This script is versioned in coderaxis/ci-workflows but runs against the
// combined working tree that holds all the repos, so it cannot assume its own
// parent directory is that tree. Walk up until a directory containing
// frontend/frontend-core is found; that is the tree.
function findTreeRoot(start) {
  let dir = start;
  for (let hops = 0; hops < 8; hops += 1) {
    if (existsSync(join(dir, 'frontend', 'frontend-core'))) return dir;
    const up = dirname(dir);
    if (up === dir) break;
    dir = up;
  }
  return start;
}

const REPO_ROOT = findTreeRoot(resolve(dirname(fileURLToPath(import.meta.url)), '..'));

// The manifest is owned by frontend-core, which owns the packages it declares.
// The second path is the pre-2026-08-06 location, kept so a tree that has not
// pulled core yet still reports something useful rather than refusing to run.
const MANIFEST = [
  join(REPO_ROOT, 'frontend/frontend-core/shared-packages.manifest.json'),
  join(REPO_ROOT, 'frontend/shared-packages.manifest.json'),
].find(existsSync) ?? join(REPO_ROOT, 'frontend/frontend-core/shared-packages.manifest.json');

const problems = [];
const notes = [];

function fail(what, detail) {
  problems.push({ what, detail });
}

function abs(p) {
  return join(REPO_ROOT, p);
}

/** Resolve a path to its real location, or null when it does not exist. */
function realOrNull(p) {
  if (!existsSync(abs(p))) return null;
  try {
    return execFileSync('realpath', [abs(p)], { encoding: 'utf8' }).trim();
  } catch {
    return abs(p);
  }
}

function gitRemote(p) {
  try {
    return execFileSync('git', ['-C', abs(p), 'remote', 'get-url', 'origin'], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch {
    return '';
  }
}

// ---------------------------------------------------------------------------

if (!existsSync(MANIFEST)) {
  console.error(`cannot run: ${MANIFEST} is missing.`);
  console.error('That manifest is the declaration this check verifies against.');
  process.exit(2);
}

function isSymlinkPath(path) {
  try {
    return lstatSync(path).isSymbolicLink();
  } catch {
    return false;
  }
}

const manifest = JSON.parse(readFileSync(MANIFEST, 'utf8'));
const packages = Object.entries(manifest.packages ?? {});
const apps = Object.entries(manifest.apps ?? {});

if (packages.length === 0) {
  console.error('cannot run: the manifest declares no packages.');
  process.exit(2);
}

// --- 1. The working tree matches what each package declares ----------------
//
// The distinction that matters is a package's NATURE, not merely that something
// exists at the path. Each shared package is now a real directory inside the
// frontend-core checkout, used where it sits, and every consumer spells it that
// one way -- so a symlink here is no longer a bridge between two layouts, it is a
// second name for the same thing and a reason for tools to disagree again.
for (const [name, spec] of packages) {
  const localPath = spec.local?.path;
  if (!localPath) {
    fail(name, 'the manifest declares no local path');
    continue;
  }
  if (!existsSync(abs(localPath))) {
    fail(name, `declared local path is missing: ${localPath}`);
    continue;
  }

  const isSymlink = lstatSync(abs(localPath)).isSymbolicLink();

  if (spec.local.expect === 'real-directory') {
    if (isSymlink) {
      fail(
        name,
        `${localPath} must be a real directory, but it is a symlink to "${readlinkSync(abs(localPath))}".\n` +
          `    Consumers now spell this package one way, so a link adds a second path that\n` +
          `    resolves to the same files -- which is how tsconfig and pnpm came to disagree\n` +
          `    before (G-25, G-30).`,
      );
      continue;
    }
    if (realOrNull(localPath) !== realOrNull(spec.ssot.path)) {
      fail(name, `${localPath} is not the declared SSOT ${spec.ssot.path}`);
    }
  } else if (spec.local.expect === 'symlink') {
    if (!isSymlink) {
      fail(
        name,
        `${localPath} must be a SYMLINK to ${spec.ssot.path}, but it is a real directory.\n` +
          `    A real directory here is a second copy of the package. Both will be edited and\n` +
          `    they will diverge; whichever one the Docker build does not use ships nothing.`,
      );
      continue;
    }
    if (realOrNull(spec.ssot.path) && realOrNull(localPath) !== realOrNull(spec.ssot.path)) {
      fail(
        name,
        `${localPath} is a symlink to "${readlinkSync(abs(localPath))}", which does not resolve\n` +
          `    to its declared SSOT ${spec.ssot.path}`,
      );
    }
  } else if (spec.local.expect === 'repo') {
    if (isSymlink) {
      fail(name, `${localPath} is declared a standalone repo but is a symlink`);
      continue;
    }
    const remote = gitRemote(localPath);
    const wanted = spec.ssot.repo;
    if (wanted && remote && !remote.includes(wanted)) {
      fail(name, `${localPath} should be a checkout of ${wanted} but its origin is "${remote}"`);
    }
    if (wanted && !remote) {
      fail(name, `${localPath} has no git origin; expected a checkout of ${wanted}`);
    }
  }
}

// --- 1b. The retired path spellings have not come back ---------------------
//
// Each package was once reachable at a path named after the repository it came
// from, because that is where the images put it. Those names are retired, and this
// check exists so a stale clone or a hand-made convenience link cannot quietly
// reintroduce the two-spellings problem: a second path that resolves to the same
// package is enough for pnpm and tsc to mean different things by one specifier.
const RETIRED_PATHS = [
  'frontend/inboxxhq-web-design',
  'frontend/inboxxhq-web-platform',
  'contracts/inboxxhq-enterprise-contracts',
];
for (const retired of RETIRED_PATHS) {
  if (!existsSync(abs(retired)) && !isSymlinkPath(abs(retired))) continue;
  const kind = isSymlinkPath(abs(retired))
    ? `a symlink to "${readlinkSync(abs(retired))}"`
    : 'a real directory';
  fail(
    retired,
    `this path is retired but exists as ${kind}.\n` +
      `    Shared packages are used at frontend/frontend-core/packages/<pkg> now, and the\n` +
      `    Docker builds assemble that same layout, so nothing needs this name. If it is a\n` +
      `    link, delete it. If it is a real directory it is probably an old clone -- check\n` +
      `    "git -C ${retired} log --not --remotes" for commits that exist nowhere else first.`,
  );
}

// --- 2. No undeclared second copy of a core-owned package ------------------
//
// This is the check that would have caught web-design on the day the subtree
// copy was merged. A package whose SSOT is a standalone repo must not ALSO exist
// as a package inside frontend-core, because such a copy has no consumer and no
// way to be noticed -- it is not built, not tested, and not shipped, so nothing
// fails when it rots.
const corePackagesDir = 'frontend/frontend-core/packages';
if (existsSync(abs(corePackagesDir))) {
  const corePackageNames = readdirSync(abs(corePackagesDir));
  for (const [name, spec] of packages) {
    if (spec.ssot.kind !== 'standalone-repo') continue;

    // e.g. "@inboxxhq/web-design" -> "web-design"
    const bare = name.split('/').pop();
    if (corePackageNames.includes(bare)) {
      fail(
        name,
        `its SSOT is the standalone repo ${spec.ssot.repo}, but a second copy exists at\n` +
          `      ${corePackagesDir}/${bare}\n` +
          `    Nothing consumes that copy: no app resolves it and no Dockerfile copies it, so it\n` +
          `    cannot fail a build no matter how far it drifts. Either finish the migration (see\n` +
          `    this package's migration.$comment in the manifest for the required ORDER) or\n` +
          `    delete the copy. Do not maintain both.`,
      );
    }
  }
}

// --- 3. Each app's Dockerfile assembles the layout a checkout has -----------
//
// The Dockerfile is what CI and deploy actually build, so it is the authority on
// where a package comes from. What it must now agree with is stronger than "the
// manifest": it must assemble the SAME layout a developer has, because that is the
// property that removed the two-spellings problem. Two things carry it -- the
// workspace root is `frontend/`, and core's packages are copied to
// `frontend-core/packages` beneath it -- and together they make every relative path
// in tsconfig mean the same directory in an image and on a laptop.
//
// Comment lines are stripped before the retired spellings are searched for, because
// the Dockerfiles deliberately explain the layout they moved away from.
for (const [appName, app] of apps) {
  const consumes = app.consumes ?? [];
  if (consumes.length === 0) continue;

  const dockerfile = join(app.path, 'Dockerfile');
  if (!existsSync(abs(dockerfile))) {
    fail(appName, `consumes shared packages but has no Dockerfile at ${dockerfile}`);
    continue;
  }
  const raw = readFileSync(abs(dockerfile), 'utf8');
  const instructions = raw
    .split('\n')
    .filter((line) => !line.trim().startsWith('#'))
    .join('\n');

  if (!instructions.includes('WORKDIR /app/frontend')) {
    fail(appName, `${dockerfile} never sets WORKDIR to /app/frontend.\n` +
      `    That directory is the workspace root in a checkout, and matching it is what makes\n` +
      `    the apps' relative tsconfig paths mean the same thing here and locally.`);
  }

  if (!instructions.includes('frontend-core/packages')) {
    fail(appName, `${dockerfile} never assembles frontend-core/packages, where every shared\n` +
      `    package is expected to be found.`);
  }

  for (const retired of RETIRED_PATHS) {
    const inImage = retired.replace(/^(frontend|contracts)\//, '');
    if (instructions.includes(retired) || instructions.includes(`/${inImage}`)) {
      fail(appName, `${dockerfile} still assembles the retired path ${retired}.\n` +
        `    Copying a shared package to a directory named after the repository it came from\n` +
        `    is what made the image and a checkout disagree, and cost each app two tsconfig\n` +
        `    globs per package (G-25, G-30).`);
    }
  }

  for (const pkgName of consumes) {
    const spec = manifest.packages[pkgName];
    if (!spec) {
      fail(appName, `consumes ${pkgName}, which the manifest does not declare`);
      continue;
    }
    // A manifest self-consistency check rather than a text search: the package is
    // reached in the image through the copied packages directory above, so what
    // matters is that its declared image path lives under it.
    if (!spec.dockerPath.startsWith('frontend/frontend-core/packages/')) {
      fail(pkgName, `its dockerPath is "${spec.dockerPath}", outside the single assembled\n` +
        `    location frontend/frontend-core/packages/.`);
    }
    if (spec.dockerPath !== spec.local.path) {
      fail(pkgName, `its image path (${spec.dockerPath}) differs from its local path\n` +
        `    (${spec.local.path}). One layout is the entire point: if these diverge, the apps\n` +
        `    need two tsconfig spellings again.`);
    }
    // And the source must appear, so that "which repo does this come from" is
    // answerable from the build rather than from folklore.
    const sourceMarker = spec.ssot.kind === 'standalone-repo' ? spec.ssot.repo : 'frontend-core';
    if (!raw.includes(sourceMarker)) {
      fail(appName, `its Dockerfile never references ${sourceMarker}, the declared source of ${pkgName}`);
    }
  }
}

// --- 3b. Each app's tsconfig names every shared package, once ---------------
//
// Angular's AOT compiler requires every component it compiles to be part of the
// TypeScript program, and sources outside the app's own src/ are not, unless a
// tsconfig include names them. So each shared package must appear -- and now it
// appears EXACTLY ONCE, at the path both the checkout and the image use.
//
// It used to need two globs per package, one per layout, and the failure mode was
// asymmetric: dropping the image spelling broke only the build that ships while
// local stayed green, and dropping the other broke only local. That is how G-25 and
// G-30 hid. Checking for the retired spelling here is not pedantry -- a leftover glob
// is the trace of someone re-adding the old layout, and it would match a directory
// that should no longer exist.
function readJsonc(file) {
  // Angular writes these files with comments, so JSON.parse alone will not do. This
  // scans character by character rather than using a regex, because a regex for block
  // comments eats the `/**/` inside globs like "src/**/*.ts" -- and those globs are
  // precisely what this check reads.
  const text = readFileSync(file, 'utf8');
  let out = '';
  let inString = false;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];

    if (inString) {
      out += char;
      if (char === '\\') {
        out += text[i + 1] ?? '';
        i += 1;
      } else if (char === '"') {
        inString = false;
      }
      continue;
    }

    if (char === '"') {
      inString = true;
      out += char;
    } else if (char === '/' && text[i + 1] === '/') {
      while (i < text.length && text[i] !== '\n') i += 1;
      out += '\n';
    } else if (char === '/' && text[i + 1] === '*') {
      const end = text.indexOf('*/', i + 2);
      i = end === -1 ? text.length : end + 1;
    } else {
      out += char;
    }
  }

  return JSON.parse(out.replace(/,(\s*[}\]])/g, '$1'));
}

for (const [appName, app] of apps) {
  const consumes = app.consumes ?? [];
  if (consumes.length === 0) continue;

  const tsconfigPath = join(app.path, 'tsconfig.app.json');
  if (!existsSync(abs(tsconfigPath))) {
    fail(appName, `consumes shared packages but has no ${tsconfigPath}`);
    continue;
  }

  let tsconfig;
  try {
    tsconfig = readJsonc(abs(tsconfigPath));
  } catch (error) {
    fail(appName, `${tsconfigPath} could not be parsed: ${error.message}`);
    continue;
  }

  const include = tsconfig.include ?? [];
  const exclude = tsconfig.exclude ?? [];
  const appDir = abs(app.path);

  for (const pkgName of consumes) {
    const spec = manifest.packages[pkgName];
    if (!spec) continue;

    const dir = spec.tsconfigSourceDir;
    if (!dir) {
      fail(pkgName, 'the manifest declares no tsconfigSourceDir, so the tsconfig check cannot run');
      continue;
    }

    const base = `${relative(appDir, abs(spec.local.path))}/${dir}/`;
    if (!include.some((glob) => glob.startsWith(base))) {
      fail(appName, `${tsconfigPath} include is missing ${pkgName} under ${base}**\n` +
        `    Without it the build fails "is missing from the TypeScript compilation", once per\n` +
        `    file reached only through a deep import.`);
    }

    // Excludes are required only where the package really ships tests or stories, so
    // this asks the package rather than assuming.
    for (const kind of ['spec', 'stories']) {
      const pkgSourceDir = join(abs(spec.local.path), dir);
      if (!existsSync(pkgSourceDir)) continue;
      let ships = false;
      try {
        ships = execFileSync('find', [pkgSourceDir, '-name', `*.${kind}.ts`, '-print', '-quit'], {
          encoding: 'utf8',
        }).trim().length > 0;
      } catch {
        ships = false;
      }
      if (ships && !exclude.some((glob) => glob.startsWith(base) && glob.endsWith(`.${kind}.ts`))) {
        fail(appName, `${tsconfigPath} exclude is missing ${base}**/*.${kind}.ts, and ` +
          `${pkgName} really ships ${kind} files.\n` +
          `    They are written against Jest globals this build has no types for.`);
      }
    }
  }

  // A glob under a retired path can only match if someone recreated it.
  for (const glob of [...include, ...exclude]) {
    for (const retired of RETIRED_PATHS) {
      const spelling = `../${retired.split('/').pop()}/`;
      if (glob.startsWith(spelling)) {
        fail(appName, `${tsconfigPath} still lists "${glob}", a glob under the retired path\n` +
          `    ${retired}. One spelling per package now; a second one is how the two layouts\n` +
          `    drifted apart before.`);
      }
    }
  }
}

// --- 4. The workspace file declares core packages by their real path --------
//
// This is a correctness invariant, not style. pnpm writes a member's dependency
// links relative to the directory it MATCHED, but creates them inside the directory
// that path RESOLVES to. When these packages were also reachable through
// frontend/inboxxhq-<pkg> symlinks two levels under frontend/, matching one made
// pnpm emit `../../node_modules/.pnpm/...` and write it into
// frontend-core/packages/<pkg>/node_modules, where `../../` means
// frontend-core/packages/. Every link dangled, rxjs and @angular/* stopped resolving
// inside the package, every type there degraded to any/unknown, and Angular could no
// longer read a component's `standalone` metadata -- surfacing as TS7006/TS18046 plus
// NG2012 "Component imports must be standalone components" against a file nobody had
// edited, which is expensive to debug from the symptom.
//
// The symlinks are gone, so the arithmetic has one answer. This still checks the real
// path is what is declared, because the same trap reopens the moment a convenience
// link is added and matched.
//
// The file is also the one both Dockerfiles copy into the image, so a member list that
// is wrong here is wrong in the build too.
{
  const wsPath = 'frontend/pnpm-workspace.yaml';
  if (!existsSync(abs(wsPath))) {
    notes.push(`${wsPath} is absent, so local installs have no workspace root`);
  } else {
    const ws = readFileSync(abs(wsPath), 'utf8');
    const declared = (line) => new RegExp(`^\\s*-\\s*["']?${line}["']?\\s*$`, 'm').test(ws);
    for (const [name, spec] of packages) {
      if (spec.ssot?.kind !== 'core-package') continue;
      const real = spec.ssot.path.replace(/^frontend\//, '');
      if (!declared(real)) {
        fail(name, `${wsPath} does not declare it by its real path "${real}".`);
      }
    }
    for (const retired of RETIRED_PATHS) {
      const spelling = retired.replace(/^frontend\//, '');
      if (spelling.includes('/')) continue; // not a frontend/ workspace member spelling
      if (declared(spelling) || declared(`!${spelling}`)) {
        fail(wsPath, `it still mentions "${spelling}", a retired path.\n` +
          `    Neither a member nor an exclusion should name it: the directory does not exist,\n` +
          `    and re-creating it is what let pnpm and tsc mean different things.`);
      }
    }
  }
}

// --- 5. The frontend-core pin is not silently behind ----------------------
//
// A pin frozen at a real commit looks deliberate, which is why this went
// unnoticed for three weeks: both apps were pinned to a July SHA while core
// moved 18 commits ahead, so five web-platform changes existed in the source a
// developer reads locally and in no deployed image. The workflow named as the
// pin's manager (bump-core-modules.yml) does not exist, so nothing advances it.
//
// Reported as a note rather than a hard failure: a pin legitimately lags while a
// core change is reviewed, and a check that cries wolf on every core commit
// would be turned off. What must never happen is that the lag is INVISIBLE.
const coreCheckout = 'frontend/frontend-core';
let coreHead = '';
if (existsSync(abs(coreCheckout))) {
  try {
    coreHead = execFileSync('git', ['-C', abs(coreCheckout), 'rev-parse', 'HEAD'], {
      encoding: 'utf8',
    }).trim();
  } catch {
    /* a missing or shallow checkout is not this check's business */
  }
}

for (const [appName, app] of apps) {
  if (!app.corePin || !coreHead) continue;
  if (!existsSync(abs(app.corePin))) {
    if ((app.consumes ?? []).some((p) => manifest.packages[p]?.ssot.kind === 'core-package')) {
      fail(appName, `consumes a frontend-core package but has no pin at ${app.corePin}`);
    }
    continue;
  }
  const pinned = (readFileSync(abs(app.corePin), 'utf8').match(/^core_sha=(\S+)/m) ?? [])[1];
  if (!pinned) {
    fail(appName, `${app.corePin} carries no core_sha`);
    continue;
  }
  if (pinned !== coreHead) {
    let behind = '';
    try {
      behind = execFileSync(
        'git',
        ['-C', abs(coreCheckout), 'rev-list', '--count', `${pinned}..HEAD`],
        { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
      ).trim();
    } catch {
      behind = 'an unknown number of';
    }
    notes.push(
      `${appName} is pinned to frontend-core ${pinned.slice(0, 9)}, which is ${behind} commits ` +
        `behind the local checkout (${coreHead.slice(0, 9)}).\n` +
        `    Those commits are in no image built from this pin. Bump ${app.corePin} when intended.`,
    );
  }
}

// ---------------------------------------------------------------------------

const declared = packages.map(([n]) => n).join(', ');

if (problems.length === 0) {
  console.log(`shared-package topology OK — ${packages.length} packages agree with the manifest,`);
  console.log(`the Dockerfiles that assemble them, and the working tree.`);
  console.log(`  checked: ${declared}`);
  for (const note of notes) console.log(`\nnote: ${note}`);
  process.exit(0);
}

console.error(`shared-package topology DRIFT — ${problems.length} problem(s).\n`);
for (const { what, detail } of problems) {
  console.error(`  ${what}:`);
  console.error(`    ${detail}\n`);
}
for (const note of notes) console.error(`note: ${note}\n`);
console.error(`See ${MANIFEST} for the declared topology and why it is declared.`);
process.exit(1);
