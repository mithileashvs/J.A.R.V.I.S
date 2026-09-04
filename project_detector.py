"""
JARVIS project detector (Section 9's "understand the project currently
being worked on").

Walks a project directory and produces a structural summary: detected
technologies (from manifest files, not guesswork), top-level layout,
and candidate "important files" (entry points, configs). This is a
SAFE, read-only operation — never writes anything outside the project
directory itself, never executes anything found there.

Deliberately shallow: this reads directory names and a small set of
manifest files (package.json, requirements.txt, etc.), not the
contents of arbitrary source files — that's analyze_code's job in a
later phase, not this one.
"""

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("jarvis-project-detector")

# Directories that are never useful to walk into and can be huge
# (venvs, node_modules, build output, VCS internals). Skipping these
# is what keeps a scan of a real project fast instead of walking
# hundreds of thousands of installed-package files.
_IGNORED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", "venv",
    ".venv", "env", ".env_dir", "dist", "build", ".next", ".cache",
    ".pytest_cache", ".mypy_cache", "site-packages", ".idea", ".vscode",
}

# manifest filename -> (technology label, how to extract deps)
_MANIFEST_SIGNALS = {
    "requirements.txt": "Python",
    "pyproject.toml": "Python",
    "Pipfile": "Python",
    "package.json": "Node.js",
    "tsconfig.json": "TypeScript",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "pom.xml": "Java (Maven)",
    "build.gradle": "Java/Kotlin (Gradle)",
    "Gemfile": "Ruby",
    "composer.json": "PHP",
    "CMakeLists.txt": "C/C++ (CMake)",
}

# Filenames that are almost always genuinely important regardless of
# language/framework — likely entry points or top-level config.
_LIKELY_IMPORTANT_FILENAMES = {
    "main.py", "app.py", "manage.py", "__main__.py",
    "index.js", "index.ts", "server.js", "app.js",
    "main.go", "main.rs", "Main.java",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".env.example", "README.md",
}


@dataclass
class ProjectSummary:
    path: str
    name: str
    technologies: list[str] = field(default_factory=list)
    structure: dict = field(default_factory=dict)   # top-level dir -> list of immediate children (names only)
    important_files: list[str] = field(default_factory=list)  # paths relative to project root
    dependency_manifests: list[str] = field(default_factory=list)


def _read_package_json_name(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("name")
    except Exception as e:
        logger.warning(f"[detector] Could not parse {path}: {e}")
        return None


def detect_project(root_path: str, max_depth: int = 2) -> ProjectSummary:
    """
    Scan root_path and return a ProjectSummary. Read-only, bounded
    depth (default 2 levels) so this stays fast even on large repos —
    Section 9's "structure" example only shows top-level folders, not
    a full recursive file tree.
    """
    root_path = os.path.abspath(root_path)

    if not os.path.isdir(root_path):
        raise NotADirectoryError(f"'{root_path}' is not a directory or does not exist")

    name = os.path.basename(root_path.rstrip(os.sep)) or root_path

    technologies: set[str] = set()
    dependency_manifests: list[str] = []
    important_files: list[str] = []
    structure: dict[str, list[str]] = {}

    # Manifest-based tech detection + important-file candidates at the
    # top level.
    for entry in sorted(os.listdir(root_path)):
        full = os.path.join(root_path, entry)

        if entry in _MANIFEST_SIGNALS and os.path.isfile(full):
            technologies.add(_MANIFEST_SIGNALS[entry])
            dependency_manifests.append(entry)

        if entry in _LIKELY_IMPORTANT_FILENAMES and os.path.isfile(full):
            important_files.append(entry)

        if entry == "package.json" and os.path.isfile(full):
            pkg_name = _read_package_json_name(full)
            if pkg_name:
                name = pkg_name

    # Bounded structure walk: top-level dirs and their immediate
    # children only (max_depth controls how many levels deep).
    def _walk(dir_path: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(os.listdir(dir_path))
        except PermissionError:
            logger.warning(f"[detector] Permission denied: {dir_path}")
            return

        rel = os.path.relpath(dir_path, root_path)
        children = []
        for entry in entries:
            if entry.startswith(".") and entry not in (".env.example",):
                # Skip dotfiles/dirs except the one we explicitly care
                # about above — most are noise (.git, .DS_Store, etc.)
                # and .git is already in _IGNORED_DIRS regardless.
                continue
            if entry in _IGNORED_DIRS:
                continue
            children.append(entry)
            full = os.path.join(dir_path, entry)
            if os.path.isdir(full) and depth < max_depth:
                _walk(full, depth + 1)

        if children or rel == ".":
            structure[rel] = children

    _walk(root_path, 0)

    return ProjectSummary(
        path=root_path,
        name=name,
        technologies=sorted(technologies),
        structure=structure,
        important_files=important_files,
        dependency_manifests=dependency_manifests,
    )


# Extensions worth grepping through for find_references(). Skips
# binaries, images, lockfiles, etc. — same "don't read the whole
# project" spirit as detect_project()'s manifest-only scan.
_SEARCHABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".rb",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".php", ".html", ".css",
    ".json", ".yaml", ".yml", ".toml", ".md", ".txt",
}

_MAX_FILES_SCANNED = 2000       # cap on how many files find_references() will open
_MAX_FILE_SIZE_BYTES = 500_000  # skip anything larger — almost certainly not hand-written source
_MAX_MATCHES = 50                # cap on returned matches


def find_references(project_path: str, symbol: str, max_results: int = 20) -> dict:
    """
    Bounded, literal (non-regex) search for `symbol` across a
    project's source files — Section 17's `find_code_reference` tool.

    Deliberately NOT a real "go to definition"/symbol-resolution
    engine — that needs per-language parsing this project doesn't
    have. This is honest, plain-text search: it finds every line
    containing the literal string, which is useful for "where else is
    this used" without pretending to understand scope, imports, or
    shadowing. Bounded by file count, file size, and match count so a
    huge repo or a very common symbol name can't turn this into an
    unbounded scan (Section 2's context-limit requirement applies to
    "understand the project" tools too, not just code analysis).
    """
    if not symbol or not symbol.strip():
        raise ValueError("symbol must be a non-empty string.")
    if not os.path.isdir(project_path):
        raise NotADirectoryError(f"'{project_path}' is not a directory or does not exist.")

    max_results = min(max_results, _MAX_MATCHES)
    matches: list[dict] = []
    files_scanned = 0
    truncated_files = False
    truncated_matches = False

    for dirpath, dirnames, filenames in os.walk(project_path):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS and not d.startswith(".")]

        for filename in sorted(filenames):
            if len(matches) >= max_results:
                truncated_matches = True
                break
            if files_scanned >= _MAX_FILES_SCANNED:
                truncated_files = True
                break

            ext = os.path.splitext(filename)[1].lower()
            if ext not in _SEARCHABLE_EXTENSIONS:
                continue

            full_path = os.path.join(dirpath, filename)
            try:
                if os.path.getsize(full_path) > _MAX_FILE_SIZE_BYTES:
                    continue
            except OSError:
                continue

            files_scanned += 1

            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    for line_no, line in enumerate(f, start=1):
                        if symbol in line:
                            matches.append({
                                "file": os.path.relpath(full_path, project_path),
                                "line": line_no,
                                "text": line.strip()[:200],
                            })
                            if len(matches) >= max_results:
                                break
            except OSError as e:
                logger.warning(f"[detector] Could not read {full_path}: {e}")
                continue

        if truncated_matches or truncated_files:
            break

    return {
        "symbol": symbol,
        "matches": matches,
        "files_scanned": files_scanned,
        "truncated": truncated_files or truncated_matches,
    }


def detect_and_save(root_path: str) -> dict:
    """
    Convenience wrapper: detect_project() + persist the result into
    project_memory.py's `projects` table. Returns the saved project
    record. This is the function Section 9's "JARVIS should understand
    the project currently being worked on" actually calls end to end.
    """
    import project_memory as pm

    summary = detect_project(root_path)
    saved = pm.upsert_project(
        path=summary.path,
        name=summary.name,
        technologies=summary.technologies,
        structure={
            "layout": summary.structure,
            "important_files": summary.important_files,
            "dependency_manifests": summary.dependency_manifests,
        },
    )
    return saved
