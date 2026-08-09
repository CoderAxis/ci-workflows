#!/usr/bin/env node
//
// Make a freshly cloned tree buildable.
//
// The frontend is a set of sibling clones that share three packages living in
// frontend-core, joined to the apps by symlinks. A symlink from one clone into
// another is not a file either clone can track, so cloning every repo is not
// enough to produce a working tree -- and until this script existed the links
// were made by hand and written down nowhere (G-38). The setup therefore existed
// only on whichever machine had already done it, and a new checkout failed in
// ways that read like code bugs rather than missing setup: pnpm resolving a
// different directory than tsc, or an app build reporting a file "missing from
// the TypeScript compilation" when the path is simply not there.
//
// Everything created here is declared in frontend-core's
// shared-packages.manifest.json. This script adds nothing of its own, so it
// cannot become a second, disagreeing description of the layout --
// check-shared-package-topology.mjs verifies the same manifest afterwards.
//
// Safe to re-run. Existing correct links are left alone; wrong ones are
// reported, and only replaced with --fix.
//
// Usage:
//   node ci-workflows/scripts/bootstrap-frontend-tree.mjs          # report
//   node ci-workflows/scripts/bootstrap-frontend-tree.mjs --fix    # create

import { existsSync, lstatSync, mkdirSync, readFileSync, readlinkSync, symlinkSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const FIX = process.argv.includes('--fix');

// The tree root is the directory holding the sibling clones. Found by walking up
// from this script (ci-workflows/scripts/) rather than assumed, so the script
// works from any cwd and survives being run through the scripts/ symlink.
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const MANIFEST = join(REPO_ROOT, 'frontend/frontend-core/shared-packages.manifest.json');

if (!existsSync(MANIFEST)) {
  console.error(
    `Cannot bootstrap: ${MANIFEST} is not there.\n\n` +
      `That file comes from frontend-core, which owns the shared packages, so it has\n` +
      `to be cloned first:\n\n` +
      `  git clone git@github.com:coderaxis/frontend-core.git ${join(REPO_ROOT, 'frontend/frontend-core')}\n`,
  );
  process.exit(1);
}

const manifest = JSON.parse(readFileSync(MANIFEST, 'utf8'));

const created = [];
const alreadyRight = [];
const wrong = [];

// One link is described by two things: where it sits, and what it points at.
// Package links carry that under local{}, the local-dev tooling links carry it at
// the top level; both are flattened here so there is a single code path.
const links = [
  ...Object.entries(manifest.packages)
    .filter(([, spec]) => spec.local?.expect === 'symlink')
    .map(([name, spec]) => ({ path: spec.local.path, target: spec.local.target, label: name })),
  ...(manifest.localDev?.symlinks ?? []).map((link) => ({ ...link, label: link.path })),
];

for (const link of links) {
  const full = join(REPO_ROOT, link.path);

  // lstat, not stat: the question is what this path IS, and stat would silently
  // answer about the target instead.
  let current = null;
  if (existsSync(full) || isLink(full)) {
    const stat = lstatSync(full);
    current = stat.isSymbolicLink() ? readlinkSync(full) : '<a real file or directory>';
  }

  if (current === link.target) {
    // Right target, but a link to a path that does not exist resolves to nothing
    // and fails later as a confusing missing-file error, so it is not "ok".
    if (!existsSync(resolve(dirname(full), link.target))) {
      wrong.push({
        ...link,
        current: `a symlink to ${link.target}, which does not exist`,
        note: 'the link is correct but dangling -- clone or update frontend-core.',
      });
      continue;
    }
    alreadyRight.push(link);
    continue;
  }

  if (current !== null) {
    // A real directory here is usually a leftover clone of a package that moved
    // into core. Deleting it could destroy unpushed commits, so this never
    // happens automatically, not even with --fix.
    wrong.push({ ...link, current });
    continue;
  }

  if (!FIX) {
    created.push({ ...link, pending: true });
    continue;
  }

  const targetExists = existsSync(resolve(dirname(full), link.target));
  if (!targetExists) {
    wrong.push({
      ...link,
      current: '<missing>',
      note: `its target ${link.target} does not exist -- is frontend-core cloned and up to date?`,
    });
    continue;
  }

  mkdirSync(dirname(full), { recursive: true });
  symlinkSync(link.target, full);
  created.push(link);
}

function isLink(path) {
  try {
    return lstatSync(path).isSymbolicLink();
  } catch {
    return false;
  }
}

for (const link of created) {
  console.log(`  ${link.pending ? 'would create' : 'created'}  ${link.path} -> ${link.target}`);
}
for (const link of alreadyRight) {
  console.log(`  ok            ${link.path} -> ${link.target}`);
}

if (wrong.length > 0) {
  console.error('\nThese paths are occupied by something other than the declared link:\n');
  for (const link of wrong) {
    console.error(`  ${link.path}`);
    console.error(`    found:    ${link.current}`);
    console.error(`    declared: a symlink to ${link.target}`);
    if (link.note) console.error(`    ${link.note}`);
    if (link.current === '<a real file or directory>') {
      console.error(
        `    Left alone deliberately: if it is an old clone of ${link.label} it may hold\n` +
          `    commits that exist nowhere else. Check "git -C ${link.path} status" and\n` +
          `    "git -C ${link.path} log --not --remotes" before removing it.`,
      );
    }
    console.error('');
  }
  process.exit(1);
}

if (!FIX && created.length > 0) {
  console.log(`\n${created.length} link(s) missing. Re-run with --fix to create them.`);
  process.exit(1);
}

console.log(
  `\nfrontend tree ready — ${links.length} declared link(s) in place.\n` +
    `Next: (cd ${join(REPO_ROOT, 'frontend')} && pnpm install), then\n` +
    `      node ${join(REPO_ROOT, 'ci-workflows/scripts/check-shared-package-topology.mjs')}`,
);
