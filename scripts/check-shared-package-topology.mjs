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

// This script is versioned in coderaxis/github-actions but runs against the
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
// exists at the path. A real directory where a symlink is declared is precisely
// the defect: it is a second copy that will drift, and it looks identical to a
// correct setup in every tool that only resolves paths.
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

  if (spec.local.expect === 'symlink') {
    if (!isSymlink) {
      fail(
        name,
        `${localPath} must be a SYMLINK to ${spec.ssot.path}, but it is a real directory.\n` +
          `    A real directory here is a second copy of the package. Both will be edited and\n` +
          `    they will diverge; whichever one the Docker build does not use ships nothing.\n` +
          `    Fix: move it aside (see contracts/archived/) and symlink it to the SSOT.`,
      );
      continue;
    }
    const linkTarget = readlinkSync(abs(localPath));
    const resolvedLocal = realOrNull(localPath);
    const resolvedSsot = realOrNull(spec.ssot.path);
    if (resolvedSsot && resolvedLocal !== resolvedSsot) {
      fail(
        name,
        `${localPath} is a symlink to "${linkTarget}", which resolves to\n` +
          `      ${resolvedLocal}\n    but its declared SSOT resolves to\n      ${resolvedSsot}`,
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
      fail(
        name,
        `${localPath} should be a checkout of ${wanted} but its origin is "${remote}"`,
      );
    }
    if (wanted && !remote) {
      fail(name, `${localPath} has no git origin; expected a checkout of ${wanted}`);
    }
  }
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

// --- 3. Each app's Dockerfile agrees with the manifest ---------------------
//
// The Dockerfile is what CI and deploy actually build, so it is the authority on
// where a package comes from. Checking the manifest against it keeps this file
// honest: a manifest that quietly disagreed with the build would be worse than
// no manifest, because it would be believed.
for (const [appName, app] of apps) {
  const consumes = app.consumes ?? [];
  if (consumes.length === 0) continue;

  const dockerfile = join(app.path, 'Dockerfile');
  if (!existsSync(abs(dockerfile))) {
    fail(appName, `consumes shared packages but has no Dockerfile at ${dockerfile}`);
    continue;
  }
  const contents = readFileSync(abs(dockerfile), 'utf8');

  for (const pkgName of consumes) {
    const spec = manifest.packages[pkgName];
    if (!spec) {
      fail(appName, `consumes ${pkgName}, which the manifest does not declare`);
      continue;
    }
    // The destination path must appear: it is what the app's tsconfig paths
    // resolve against, in both layouts.
    if (!contents.includes(spec.dockerPath)) {
      fail(
        appName,
        `its Dockerfile never mentions ${spec.dockerPath}, where ${pkgName} is declared to be assembled`,
      );
    }
    // And the source must appear, so that "which repo does this come from" is
    // answerable from the build rather than from folklore.
    const sourceMarker = spec.ssot.kind === 'standalone-repo' ? spec.ssot.repo : 'frontend-core';
    if (!contents.includes(sourceMarker)) {
      fail(appName, `its Dockerfile never references ${sourceMarker}, the declared source of ${pkgName}`);
    }
  }
}

// --- 3b. Each app's tsconfig names every shared package, in BOTH spellings --
//
// Angular's AOT compiler requires every component it compiles to be part of the
// TypeScript program, and sources outside the app's own src/ are not, unless a
// tsconfig include names them. The path differs between the two layouts this app
// builds in -- locally the workspace path is a symlink and TypeScript reports the
// resolved frontend-core path, while in the image it is a real directory and no
// frontend-core exists -- so both spellings must be listed. A glob matching
// nothing is not an error, which is what makes carrying both safe.
//
// This is checked rather than merely documented because dropping the image
// spelling breaks only the build that ships while local stays green, and the
// reverse breaks only local. That asymmetry is exactly how G-25 and G-30 hid, and
// while the CI compile gate is down (G-39) nothing else would catch either.
// Angular writes these files with comments, so JSON.parse alone will not do.
// This scans character by character instead of using a regex, because a regex for
// block comments eats the `/**/` inside globs like "src/**/*.ts" -- and those globs
// are precisely what this check reads.
function readJsonc(file) {
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

    const spellings = {
      'the frontend-core path (used locally, where the workspace path is a symlink)':
        relative(appDir, abs(spec.ssot.path)),
      'the workspace path (used in the image, where it is a real directory)':
        relative(appDir, abs(spec.dockerPath)),
    };

    for (const [why, prefix] of Object.entries(spellings)) {
      const base = `${prefix}/${dir}/`;
      if (!include.some((glob) => glob.startsWith(base))) {
        fail(appName, `${tsconfigPath} include is missing ${pkgName} under ${base}**\n` +
          `    That is ${why}.\n` +
          `    Without it the build fails "is missing from the TypeScript compilation",\n` +
          `    once per file reached only through a deep import.`);
      }

      // Excludes are required only where the package really ships tests or
      // stories, so this asks the package rather than assuming.
      for (const kind of ['spec', 'stories']) {
        const pkgSourceDir = join(abs(spec.ssot.path), dir);
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
          fail(appName, `${tsconfigPath} exclude is missing ${base}**/*.${kind}.ts, ` +
            `and ${pkgName} really ships ${kind} files.\n` +
            `    They are written against Jest globals this build has no types for.`);
        }
      }
    }
  }
}

// --- 4. The local pnpm workspace declares core packages by their real path --
//
// This is a correctness invariant, not style. pnpm writes a member's dependency
// links relative to the directory it MATCHED, but creates them inside the
// directory that path RESOLVES to. Matching frontend/inboxxhq-<pkg>, a symlink
// two levels under frontend/, makes pnpm emit `../../node_modules/.pnpm/...` and
// write it into frontend-core/packages/<pkg>/node_modules, where `../../` means
// frontend-core/packages/. Every link dangles, rxjs and @angular/* stop
// resolving inside the package, every type there degrades to any/unknown, and
// Angular can no longer read a component's `standalone` metadata. It surfaces as
// TS7006/TS18046 plus NG2012 "Component imports must be standalone components"
// against a file nobody edited, which is an expensive thing to debug from the
// symptom. Declaring the real path makes pnpm emit the correct `../../../../`.
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
      const symlink = spec.local?.path?.replace(/^frontend\//, '');
      if (!declared(real)) {
        fail(name, `${wsPath} does not declare it by its real path "${real}".\n` +
          `    Without that pnpm may match the symlink instead and write dependency\n` +
          `    links at the wrong depth, leaving every one of them dangling.`);
      }
      if (symlink && !symlink.includes('/') && !declared(`!${symlink}`)) {
        fail(name, `${wsPath} does not exclude the symlink spelling "!${symlink}",\n` +
          `    so the package can be matched twice and pnpm may pick the symlink.`);
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
