# chunker.py
# AST-aware code chunking using tree-sitter.
#
# Instead of naively splitting code into fixed-size text windows (which cuts
# functions/classes in half and destroys context), we parse each file into an
# AST and slice along function/class/method boundaries. Each chunk keeps
# metadata (file path, line range, symbol name) so the retriever can later
# show "source: auth.py, lines 45-62" instead of a raw text blob.

import os
from dataclasses import dataclass, field
from typing import List, Optional

from tree_sitter import Language, Parser

@dataclass
class CodeChunk:
    """A single retrievable unit of code with attached metadata."""
    content: str
    file_path: str
    start_line: int
    end_line: int
    symbol_name: str
    symbol_type: str  # "function", "method", "class", "constant", "module" (fallback)
    language: str

    def to_metadata(self) -> dict:
        """Metadata dict for storage in the vector DB (Chroma requires flat types)."""
        return {
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "symbol_name": self.symbol_name,
            "symbol_type": self.symbol_type,
            "language": self.language,
        }

    def to_embedding_text(self) -> str:
        """
        Text that actually gets embedded. Prepending the file path + symbol name
        helps retrieval match queries like "the login function" or "auth.py".
        """
        return f"# File: {self.file_path}\n# {self.symbol_type}: {self.symbol_name}\n\n{self.content}"


LANGUAGE_CONFIG = {}

try:
    import tree_sitter_python as tspython
    py_lang = Language(tspython.language())
    LANGUAGE_CONFIG[".py"] = {
        "language": py_lang,
        "chunk_node_types": {"function_definition", "class_definition"},
    }
except ImportError:
    pass

try:
    import tree_sitter_javascript as tsjavascript
    js_lang = Language(tsjavascript.language())
    js_types = {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "arrow_function",
    }
    LANGUAGE_CONFIG[".js"] = {"language": js_lang, "chunk_node_types": js_types}
    LANGUAGE_CONFIG[".jsx"] = {"language": js_lang, "chunk_node_types": js_types}
except ImportError:
    pass

try:
    import tree_sitter_typescript as tstypescript
    ts_types = {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "arrow_function",
    }
    if hasattr(tstypescript, "language_typescript"):
        LANGUAGE_CONFIG[".ts"] = {
            "language": Language(tstypescript.language_typescript()),
            "chunk_node_types": ts_types,
        }
    if hasattr(tstypescript, "language_tsx"):
        LANGUAGE_CONFIG[".tsx"] = {
            "language": Language(tstypescript.language_tsx()),
            "chunk_node_types": ts_types,
        }
except ImportError:
    pass

try:
    import tree_sitter_c as tsc
    c_lang = Language(tsc.language())
    c_types = {"function_definition", "struct_specifier"}
    LANGUAGE_CONFIG[".c"] = {"language": c_lang, "chunk_node_types": c_types}
    LANGUAGE_CONFIG[".h"] = {"language": c_lang, "chunk_node_types": c_types}
except ImportError:
    pass

try:
    import tree_sitter_cpp as tscpp
    cpp_lang = Language(tscpp.language())
    cpp_types = {
        "function_definition",
        "class_specifier",
        "struct_specifier",
        "template_declaration",
    }
    # .ino files are Arduino sketches. They're preprocessed (implicit
    # function prototypes added, then wrapped in a .cpp) before compiling,
    # but the source as written on disk is valid C++ as far as tree-sitter's
    # parser is concerned, so we can parse it directly with the same C++
    # grammar without needing the Arduino preprocessing step.
    for ext in (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".ino"):
        LANGUAGE_CONFIG[ext] = {"language": cpp_lang, "chunk_node_types": cpp_types}
except ImportError:
    pass

# Human-readable language names, used for fallback/error messaging when a
# repo has no chunkable files (see ingest.py's UnsupportedRepoError).
SUPPORTED_LANGUAGES_DISPLAY = [
    "Python (.py)",
    "JavaScript (.js, .jsx)",
    "TypeScript (.ts, .tsx)",
    "C (.c, .h)",
    "C++ (.cpp, .cc, .cxx, .hpp, .hh, .hxx)",
    "Arduino (.ino)",
]

# Files/dirs we never want to walk into or chunk
IGNORED_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
    ".next", ".turbo", "target", "vendor", ".idea", ".vscode", "coverage",
    "site-packages", "egg-info",
}
IGNORED_FILE_SUFFIXES = (".min.js", ".lock", ".map")
MAX_FILE_SIZE_BYTES = 512_000  # skip anything absurdly large (generated/minified/bundled)

# A class larger than this (in lines) gets split into per-method chunks
# instead of kept as one giant blob. Small classes stay whole since splitting
# a 15-line class into 3 fragments would lose more context than it gains.
MAX_CLASS_LINES_BEFORE_SPLIT = 80


def _get_parser(language: Language) -> Parser:
    parser = Parser(language)
    return parser


# Leaf node types that directly hold a usable name.
_LEAF_NAME_TYPES = (
    "identifier", "property_identifier", "type_identifier",
    "field_identifier", "destructor_name", "operator_name",
)
# Wrapper/declarator node types we recurse into to find a name (C/C++ needs
# this because a function's name sits inside a declarator subtree, not as a
# direct child of function_definition -- e.g. `int *foo()` or
# `Widget::~Widget()`).
_DECLARATOR_WRAPPER_TYPES = (
    "function_declarator", "pointer_declarator", "reference_declarator",
    "array_declarator", "parenthesized_declarator", "qualified_identifier",
)

# Node types that represent a simple statement wrapping an assignment, e.g.
# Python's `expression_statement` -> `assignment`. Used to catch top-level
# constants (COMMON_TICKERS = {...}) that chunk_node_types alone would
# silently skip, since they're neither a function nor a class.
_ASSIGNMENT_STATEMENT_TYPES = {"expression_statement"}
_ASSIGNMENT_NODE_TYPES = {"assignment"}


def _extract_symbol_name(node, source_bytes: bytes) -> str:
    """Best-effort extraction of a function/class/struct name from an AST node."""
    # C/C++ function_definition nodes expose a named 'declarator' field that
    # points straight at the name-bearing subtree, skipping the return type
    # (which would otherwise be mistaken for the name, e.g. `T convert(...)`
    # or `int* getData()`). Python/JS/TS nodes don't have this field, so this
    # is a no-op for them and falls through to the generic scan below.
    declarator = node.child_by_field_name("declarator")
    if declarator is not None:
        name = _extract_symbol_name(declarator, source_bytes)
        if name != "<anonymous>":
            return name

    for child in node.children:
        if child.type in _LEAF_NAME_TYPES:
            return source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")

    for child in node.children:
        if child.type in _DECLARATOR_WRAPPER_TYPES:
            name = _extract_symbol_name(child, source_bytes)
            if name != "<anonymous>":
                return name

    return "<anonymous>"


def chunk_file(file_path: str, source_code: str, language: Language,
               chunk_node_types: set, lang_label: str) -> List[CodeChunk]:
    """
    Parse a single file's source and slice it into CodeChunks along
    function/class boundaries. Falls back to whole-file chunking if no
    matching nodes are found (e.g. a file that's just top-level statements).
    """
    parser = _get_parser(language)
    source_bytes = source_code.encode("utf-8")
    tree = parser.parse(source_bytes)

    chunks: List[CodeChunk] = []

    # "Container" nodes (things with a body full of methods/fields). Structs
    # are included alongside classes since C++ structs can carry methods too,
    # and even plain C/C++ structs are worth their own chunk for retrieval.
    _CLASS_TYPES = ("class_definition", "class_declaration", "class_specifier")
    _STRUCT_TYPES = ("struct_specifier",)
    is_class_node = lambda n: n.type in _CLASS_TYPES or n.type in _STRUCT_TYPES
    is_method_node = lambda n: n.type in ("function_definition", "method_definition")
    container_symbol_type = lambda n: "struct" if n.type in _STRUCT_TYPES else "class"

    # Comment style for the "rest chunked separately" marker left in split
    # class/struct signature chunks.
    comment_prefix = "#" if lang_label == "py" else "//"

    def make_chunk(node, symbol_type, symbol_name) -> Optional[CodeChunk]:
        text = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        if len(text.strip()) < 20:
            return None
        return CodeChunk(
            content=text,
            file_path=file_path,
            start_line=node.start_point[0] + 1,  # tree-sitter is 0-indexed
            end_line=node.end_point[0] + 1,
            symbol_name=symbol_name,
            symbol_type=symbol_type,
            language=lang_label,
        )

    def class_signature_chunk(node, symbol_name, symbol_type) -> Optional[CodeChunk]:
        """
        For large classes/structs we split by method, but we still want ONE
        small chunk representing "what is this" - signature + docstring +
        attribute declarations, but NOT full method bodies. This keeps
        class-level questions retrievable without dragging in a huge blob.
        """
        body = None
        for child in node.children:
            if child.type in ("block", "class_body", "field_declaration_list"):
                body = child
                break
        if body is None:
            return make_chunk(node, symbol_type, symbol_name)

        first_method = next((c for c in body.children if is_method_node(c)), None)
        end_byte = first_method.start_byte if first_method else body.end_byte
        text = source_bytes[node.start_byte:end_byte].decode("utf-8", errors="replace")
        if len(text.strip()) < 20:
            return None
        return CodeChunk(
            content=text.rstrip() + f"\n    {comment_prefix} ... methods chunked separately, see below ...",
            file_path=file_path,
            start_line=node.start_point[0] + 1,
            end_line=(first_method.start_point[0] if first_method else node.end_point[0]) + 1,
            symbol_name=symbol_name,
            symbol_type=symbol_type,
            language=lang_label,
        )

    def walk(node, inside_class=False):
        # C++ template functions (`template<typename T> T foo(...) {...}`)
        # wrap a function_definition inside a template_declaration. Chunk the
        # whole template_declaration (so the `template<...>` line is kept)
        # and stop, so we don't also emit a near-duplicate inner chunk.
        if node.type == "template_declaration":
            inner_func = next(
                (c for c in node.children if c.type == "function_definition"), None
            )
            if inner_func is not None:
                symbol_name = _extract_symbol_name(inner_func, source_bytes)
                symbol_type = "method" if inside_class else "function"
                chunk = make_chunk(node, symbol_type, symbol_name)
                if chunk:
                    chunks.append(chunk)
                return
            # Not a template function (e.g. a template class) - fall through
            # and let the normal walk handle whatever it wraps.

        if is_class_node(node):
            symbol_name = _extract_symbol_name(node, source_bytes)
            symbol_type = container_symbol_type(node)
            num_lines = node.end_point[0] - node.start_point[0]

            if num_lines > MAX_CLASS_LINES_BEFORE_SPLIT:
                sig_chunk = class_signature_chunk(node, symbol_name, symbol_type)
                if sig_chunk:
                    chunks.append(sig_chunk)
                for child in node.children:
                    walk(child, inside_class=True)
                return
            else:
                chunk = make_chunk(node, symbol_type, symbol_name)
                if chunk:
                    chunks.append(chunk)
                return

        if node.type in chunk_node_types and (is_method_node(node) or node.type == "arrow_function"):
            symbol_name = _extract_symbol_name(node, source_bytes)
            symbol_type = "method" if inside_class else "function"
            chunk = make_chunk(node, symbol_type, symbol_name)
            if chunk:
                chunks.append(chunk)
            return

        # Top-level constant assignments (e.g. COMMON_TICKERS = {...}).
        # Only at module level -- inside_class is False here since class
        # bodies get walked separately via class_signature_chunk's method
        # split, and we don't want e.g. self.x = ... inside a method to be
        # picked up (those live inside function_definition, which already
        # returned above and never reaches this point).
        if not inside_class and node.type in _ASSIGNMENT_STATEMENT_TYPES:
            assign_node = next(
                (c for c in node.children if c.type in _ASSIGNMENT_NODE_TYPES), None
            )
            if assign_node is not None:
                left = assign_node.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    const_name = source_bytes[left.start_byte:left.end_byte].decode("utf-8", errors="replace")
                    # Convention check: only treat ALL_CAPS names as
                    # meaningful constants worth their own chunk -- skips
                    # incidental top-level assignments like `app = Flask(__name__)`
                    if const_name.isupper():
                        chunk = make_chunk(node, "constant", const_name)
                        if chunk:
                            chunks.append(chunk)
                        return

        for child in node.children:
            walk(child, inside_class=inside_class)

    walk(tree.root_node)

    # Fallback: no function/class-level nodes found at all (rare, but happens
    # with config-style files or files that are just top-level script code)
    if not chunks and source_code.strip():
        chunks.append(CodeChunk(
            content=source_code,
            file_path=file_path,
            start_line=1,
            end_line=source_code.count("\n") + 1,
            symbol_name=os.path.basename(file_path),
            symbol_type="module",
            language=lang_label,
        ))

    return chunks


def should_skip_path(path: str) -> bool:
    parts = path.split(os.sep)
    if any(p in IGNORED_DIRS for p in parts):
        return True
    if path.endswith(IGNORED_FILE_SUFFIXES):
        return True
    return False


def chunk_repository(repo_path: str) -> List[CodeChunk]:
    """
    Walk a cloned repo directory, chunk every supported source file, and
    return a flat list of CodeChunks with file/line metadata attached.
    """
    all_chunks: List[CodeChunk] = []

    for root, dirs, files in os.walk(repo_path):
        # prune ignored directories in-place so os.walk doesn't descend into them
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]

        for fname in files:
            ext = os.path.splitext(fname)[1]
            if ext not in LANGUAGE_CONFIG:
                continue

            abs_path = os.path.join(root, fname)
            rel_path = os.path.relpath(abs_path, repo_path)

            if should_skip_path(rel_path):
                continue
            if os.path.getsize(abs_path) > MAX_FILE_SIZE_BYTES:
                continue

            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    source = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            if not source.strip():
                continue

            config = LANGUAGE_CONFIG[ext]
            lang_label = ext.lstrip(".")
            try:
                file_chunks = chunk_file(
                    file_path=rel_path,
                    source_code=source,
                    language=config["language"],
                    chunk_node_types=config["chunk_node_types"],
                    lang_label=lang_label,
                )
            except Exception as e:
                # Don't let one malformed file kill the whole ingestion run
                print(f"  [warn] failed to parse {rel_path}: {e}")
                continue

            all_chunks.extend(file_chunks)

    return all_chunks


# Friendly display names for extensions we recognize but don't (yet) chunk,
# used purely to build a helpful "here's what this repo actually contains"
# message when ingestion finds nothing chunkable. Not exhaustive - just the
# common cases worth naming explicitly.
_KNOWN_UNSUPPORTED_LANGUAGES = {
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
    ".cs": "C#", ".swift": "Swift", ".m": "Objective-C", ".mm": "Objective-C++",
    ".scala": "Scala", ".dart": "Dart", ".lua": "Lua", ".r": "R",
    ".ex": "Elixir", ".exs": "Elixir", ".erl": "Erlang", ".hs": "Haskell",
    ".clj": "Clojure", ".vue": "Vue", ".svelte": "Svelte", ".sh": "Shell",
    ".sql": "SQL", ".pl": "Perl", ".zig": "Zig", ".jl": "Julia",
}


def detect_repo_languages(repo_path: str) -> dict:
    """
    Walk a repo and count source files by extension, split into "supported"
    (something in LANGUAGE_CONFIG) and "other" (recognized-but-unsupported,
    or unknown). Used to build a helpful message when a repo yields zero
    chunkable files, e.g. "this looks like a Go repo, which isn't supported
    yet — try a Python/JS/TS/C/C++ repo instead."

    Returns {"supported": {ext: count}, "other": {label: count}}, both sorted
    by descending count, capped to the top few entries each.
    """
    supported_counts: dict = {}
    other_counts: dict = {}

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if not ext:
                continue
            if ext in LANGUAGE_CONFIG:
                supported_counts[ext] = supported_counts.get(ext, 0) + 1
            elif ext in _KNOWN_UNSUPPORTED_LANGUAGES:
                label = _KNOWN_UNSUPPORTED_LANGUAGES[ext]
                other_counts[label] = other_counts.get(label, 0) + 1

    top_supported = dict(sorted(supported_counts.items(), key=lambda kv: -kv[1])[:5])
    top_other = dict(sorted(other_counts.items(), key=lambda kv: -kv[1])[:5])
    return {"supported": top_supported, "other": top_other}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python chunker.py <path-to-repo>")
        sys.exit(1)

    chunks = chunk_repository(sys.argv[1])
    print(f"Extracted {len(chunks)} chunks\n")
    for c in chunks[:10]:
        print(f"[{c.language}] {c.symbol_type} '{c.symbol_name}' — {c.file_path}:{c.start_line}-{c.end_line}")