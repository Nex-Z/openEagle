// Extracted path helpers; boundary prefix bug corrected from local Node migration; see docs/v2/REUSE_AUDIT.md.
import fs from "node:fs";
import path from "node:path";

function isInside(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate);
  return relative === "" || ((relative !== ".." && !relative.startsWith(`..${path.sep}`)) && !path.isAbsolute(relative));
}

function nearestExistingParent(candidate: string): string {
  let current = candidate;
  while (!fs.existsSync(current)) {
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return current;
}

function lstatIfPresent(candidate: string): fs.Stats | undefined {
  try {
    return fs.lstatSync(candidate);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw error;
  }
}

function mutationPathParts(root: string, candidate: string): string[] {
  const relative = path.relative(root, candidate);
  if (relative === "") return [];
  return relative.split(path.sep).filter(Boolean);
}

/**
 * Mutating tools deliberately reject every symbolic-link component, including
 * links whose current target is still inside the workspace. Node does not
 * expose portable openat(2)-style APIs, so following a link here would reopen
 * a check/use race when a different process retargets it.
 */
export function revalidateWorkspaceMutationPath(
  workspaceRoot: string,
  candidate: string,
  input = candidate,
): void {
  const root = fs.realpathSync.native(path.resolve(workspaceRoot));
  const resolvedCandidate = path.resolve(candidate);
  if (!isInside(root, resolvedCandidate)) {
    throw new Error(`path is outside the workspace: ${input}`);
  }

  let current = root;
  const parts = mutationPathParts(root, resolvedCandidate);
  for (let index = 0; index < parts.length; index += 1) {
    current = path.join(current, parts[index]!);
    const stat = lstatIfPresent(current);
    if (!stat) return;
    if (stat.isSymbolicLink()) {
      throw new Error(`mutable paths cannot contain symbolic links: ${input}`);
    }
    if (index < parts.length - 1 && !stat.isDirectory()) {
      throw new Error(`mutable path parent is not a directory: ${input}`);
    }
    const realCurrent = fs.realpathSync.native(current);
    if (!isInside(root, realCurrent)) {
      throw new Error(`path escapes the workspace during mutation: ${input}`);
    }
  }
}

export function resolveWorkspaceMutationPath(workspaceRoot: string, input = "."): string {
  const candidate = resolveWorkspacePath(workspaceRoot, input);
  revalidateWorkspaceMutationPath(workspaceRoot, candidate, input);
  return candidate;
}

export interface WorkspaceDirectorySnapshotEntry {
  readonly path: string;
  readonly device: bigint;
  readonly inode: bigint;
}

/** Capture every existing directory identity from the canonical root to parent. */
export function captureWorkspaceDirectorySnapshot(
  workspaceRoot: string,
  directory: string,
): readonly WorkspaceDirectorySnapshotEntry[] {
  const root = fs.realpathSync.native(path.resolve(workspaceRoot));
  const candidate = path.resolve(directory);
  revalidateWorkspaceMutationPath(root, candidate);
  const rootStat = fs.lstatSync(root, { bigint: true });
  const result: WorkspaceDirectorySnapshotEntry[] = [{
    path: root,
    device: rootStat.dev,
    inode: rootStat.ino,
  }];
  let current = root;
  for (const part of mutationPathParts(root, candidate)) {
    current = path.join(current, part);
    const stat = lstatIfPresent(current);
    if (!stat) break;
    if (!stat.isDirectory()) throw new Error(`mutable path parent is not a directory: ${directory}`);
    const bigintStat = fs.lstatSync(current, { bigint: true });
    result.push({ path: current, device: bigintStat.dev, inode: bigintStat.ino });
  }
  return result;
}

export function revalidateWorkspaceDirectorySnapshot(
  workspaceRoot: string,
  snapshot: readonly WorkspaceDirectorySnapshotEntry[],
): void {
  for (const entry of snapshot) {
    revalidateWorkspaceMutationPath(workspaceRoot, entry.path);
    const stat = fs.lstatSync(entry.path, { bigint: true });
    if (!stat.isDirectory() || stat.dev !== entry.device || stat.ino !== entry.inode) {
      throw new Error(`workspace directory changed during mutation: ${entry.path}`);
    }
  }
}

/** Build missing directories one component at a time and reject link swaps. */
export function ensureWorkspaceDirectory(workspaceRoot: string, directory: string): void {
  const root = fs.realpathSync.native(path.resolve(workspaceRoot));
  const candidate = path.resolve(directory);
  revalidateWorkspaceMutationPath(root, candidate);
  let current = root;
  for (const part of mutationPathParts(root, candidate)) {
    const parent = current;
    current = path.join(current, part);
    const stat = lstatIfPresent(current);
    if (!stat) {
      revalidateWorkspaceMutationPath(root, parent);
      try {
        fs.mkdirSync(current);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      }
    }
    revalidateWorkspaceMutationPath(root, current);
    if (!fs.lstatSync(current).isDirectory()) {
      throw new Error(`mutable path parent is not a directory: ${directory}`);
    }
  }
}

export function resolveWorkspacePath(workspaceRoot: string, input = "."): string {
  const root = fs.realpathSync.native(path.resolve(workspaceRoot));
  const candidate = path.resolve(root, input || ".");
  if (!isInside(root, candidate)) {
    throw new Error(`path is outside the workspace: ${input}`);
  }
  const existingParent = nearestExistingParent(candidate);
  const realParent = fs.realpathSync.native(existingParent);
  if (!isInside(root, realParent)) {
    throw new Error(`path escapes the workspace through a link: ${input}`);
  }
  return candidate;
}

