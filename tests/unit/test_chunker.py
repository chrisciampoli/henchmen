"""Unit tests for the code chunking engine."""

import textwrap

from henchmen.dossier.chunker import CodeChunk, chunk_file, chunk_files, should_skip_file

# ---------------------------------------------------------------------------
# Skip rules
# ---------------------------------------------------------------------------


class TestSkipRules:
    def test_skips_lockfile(self):
        assert should_skip_file("package-lock.json") is True

    def test_skips_pnpm_lock(self):
        assert should_skip_file("pnpm-lock.yaml") is True

    def test_skips_binary_extension(self):
        assert should_skip_file("logo.png") is True
        assert should_skip_file("font.woff2") is True

    def test_skips_dot_git_dir(self):
        assert should_skip_file(".git/config") is True

    def test_skips_node_modules(self):
        assert should_skip_file("node_modules/react/index.js") is True

    def test_allows_python_file(self):
        assert should_skip_file("src/auth/login.py") is False

    def test_allows_typescript_file(self):
        assert should_skip_file("src/components/App.tsx") is False

    def test_allows_json_file(self):
        assert should_skip_file("tsconfig.json") is False

    def test_skips_large_file(self):
        assert should_skip_file("big.py", file_size=200_000) is True

    def test_allows_normal_size_file(self):
        assert should_skip_file("normal.py", file_size=5_000) is False

    def test_skips_unsupported_extension(self):
        assert should_skip_file("image.svg") is True
        assert should_skip_file("data.bin") is True


# ---------------------------------------------------------------------------
# CodeChunk model
# ---------------------------------------------------------------------------


class TestCodeChunkModel:
    def test_code_chunk_creation(self):
        chunk = CodeChunk(
            file_path="src/foo.py",
            start_line=1,
            end_line=10,
            symbol_name="foo",
            language="python",
            content="def foo(): pass",
            chunk_type="function",
        )
        assert chunk.file_path == "src/foo.py"
        assert chunk.chunk_type == "function"

    def test_code_chunk_optional_symbol_name(self):
        chunk = CodeChunk(
            file_path="config.yaml",
            start_line=1,
            end_line=5,
            symbol_name=None,
            language="yaml",
            content="key: value",
            chunk_type="fixed",
        )
        assert chunk.symbol_name is None


# ---------------------------------------------------------------------------
# Python AST chunking
# ---------------------------------------------------------------------------


class TestPythonChunking:
    def test_chunks_top_level_function(self):
        source = textwrap.dedent("""\
            def greet(name: str) -> str:
                return f"Hello, {name}"
        """)
        chunks = chunk_file("hello.py", source)
        assert len(chunks) == 1
        assert chunks[0].symbol_name == "greet"
        assert chunks[0].chunk_type == "function"
        assert chunks[0].start_line == 1
        assert chunks[0].language == "python"

    def test_chunks_class_and_methods(self):
        source = textwrap.dedent("""\
            class UserService:
                def __init__(self, db):
                    self.db = db

                def get_user(self, user_id: int):
                    return self.db.get(user_id)

                def delete_user(self, user_id: int):
                    self.db.delete(user_id)
        """)
        chunks = chunk_file("service.py", source)
        names = [c.symbol_name for c in chunks]
        assert "UserService" in names
        assert "UserService.__init__" in names
        assert "UserService.get_user" in names
        assert "UserService.delete_user" in names

    def test_chunks_multiple_functions(self):
        source = textwrap.dedent("""\
            def add(a, b):
                return a + b

            def subtract(a, b):
                return a - b
        """)
        chunks = chunk_file("math.py", source)
        names = [c.symbol_name for c in chunks]
        assert "add" in names
        assert "subtract" in names

    def test_handles_syntax_error_gracefully(self):
        source = "def broken(\n    # missing closing paren"
        chunks = chunk_file("broken.py", source)
        assert len(chunks) >= 1
        assert chunks[0].chunk_type == "fixed"

    def test_empty_file_returns_empty(self):
        chunks = chunk_file("empty.py", "")
        assert chunks == []


# ---------------------------------------------------------------------------
# TypeScript/JavaScript regex chunking
# ---------------------------------------------------------------------------


class TestTypeScriptChunking:
    def test_chunks_function_declaration(self):
        source = textwrap.dedent("""\
            function greet(name: string): string {
                return `Hello, ${name}`;
            }
        """)
        chunks = chunk_file("hello.ts", source)
        assert len(chunks) >= 1
        assert any(c.symbol_name == "greet" for c in chunks)
        assert chunks[0].language == "typescript"

    def test_chunks_arrow_function_export(self):
        source = textwrap.dedent("""\
            export const fetchUser = async (id: number) => {
                const res = await fetch(`/api/users/${id}`);
                return res.json();
            };
        """)
        chunks = chunk_file("api.ts", source)
        assert any(c.symbol_name == "fetchUser" for c in chunks)

    def test_chunks_class(self):
        source = textwrap.dedent("""\
            export class UserService {
                constructor(private db: Database) {}

                async getUser(id: number) {
                    return this.db.get(id);
                }
            }
        """)
        chunks = chunk_file("service.ts", source)
        assert any(c.symbol_name == "UserService" for c in chunks)

    def test_chunks_react_component(self):
        source = textwrap.dedent("""\
            export default function Dashboard({ user }: Props) {
                return <div>Hello {user.name}</div>;
            }
        """)
        chunks = chunk_file("Dashboard.tsx", source)
        assert any(c.symbol_name == "Dashboard" for c in chunks)

    def test_jsx_extension_uses_typescript_chunker(self):
        source = "function App() { return <div/>; }"
        chunks = chunk_file("App.jsx", source)
        assert chunks[0].language == "javascript"


# ---------------------------------------------------------------------------
# Fixed-size fallback chunking
# ---------------------------------------------------------------------------


class TestFixedSizeChunking:
    def test_small_file_single_chunk(self):
        source = "key: value\nother: data\n"
        chunks = chunk_file("config.yaml", source)
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "fixed"
        assert chunks[0].language == "yaml"

    def test_large_file_multiple_chunks(self):
        lines = [f"line_{i}: value_{i}" for i in range(200)]
        source = "\n".join(lines)
        chunks = chunk_file("big.yaml", source)
        assert len(chunks) >= 1
        assert all(c.chunk_type == "fixed" for c in chunks)

    def test_chunks_respect_line_boundaries(self):
        lines = [f"line {i}" for i in range(100)]
        source = "\n".join(lines)
        chunks = chunk_file("data.md", source)
        for chunk in chunks:
            assert not chunk.content.startswith("\n")

    def test_chunk_file_preserves_file_path(self):
        chunks = chunk_file("docs/README.md", "# Hello\nWorld\n")
        assert all(c.file_path == "docs/README.md" for c in chunks)


# ---------------------------------------------------------------------------
# chunk_files (batch helper)
# ---------------------------------------------------------------------------


class TestChunkFiles:
    def test_chunk_files_processes_dict(self):
        files = {
            "hello.py": "def greet(): pass\n",
            "config.yaml": "key: value\n",
        }
        all_chunks = chunk_files(files)
        paths = {c.file_path for c in all_chunks}
        assert "hello.py" in paths
        assert "config.yaml" in paths

    def test_chunk_files_skips_lockfiles(self):
        files = {
            "hello.py": "def greet(): pass\n",
            "package-lock.json": '{"name": "x"}',
        }
        all_chunks = chunk_files(files)
        paths = {c.file_path for c in all_chunks}
        assert "hello.py" in paths
        assert "package-lock.json" not in paths


# ---------------------------------------------------------------------------
# Module-level Python code (no top-level def/class)
# ---------------------------------------------------------------------------


class TestPythonModuleLevelCode:
    def test_def_less_module_still_produces_chunks(self):
        """`where is MAX_RETRIES defined` must be answerable by RAG."""
        content = textwrap.dedent(
            """\
            import os

            MAX_RETRIES = 3
            TIMEOUT = 30
            __all__ = ["MAX_RETRIES", "TIMEOUT"]
            """
        )
        chunks = chunk_file("constants.py", content)
        assert chunks
        assert "MAX_RETRIES" in chunks[0].content
        assert chunks[0].chunk_type == "fixed"

    def test_empty_package_init_produces_no_chunks(self):
        assert chunk_file("pkg/__init__.py", "\n\n   \n") == []

    def test_constants_between_functions_are_indexed(self):
        content = textwrap.dedent(
            """\
            def first():
                return 1

            SENTINEL_VALUE = "between"

            def second():
                return 2
            """
        )
        chunks = chunk_file("mod.py", content)
        assert any("SENTINEL_VALUE" in c.content and c.chunk_type == "fixed" for c in chunks)
        assert {c.symbol_name for c in chunks} >= {"first", "second"}

    def test_leading_imports_are_indexed(self):
        content = textwrap.dedent(
            """\
            import os
            from pathlib import Path


            def go():
                return Path(os.getcwd())
            """
        )
        chunks = chunk_file("mod.py", content)
        fixed = [c for c in chunks if c.chunk_type == "fixed"]
        assert fixed
        assert "from pathlib import Path" in fixed[0].content
        assert fixed[0].start_line == 1

    def test_line_numbers_of_gap_chunks_are_absolute(self):
        content = "def a():\n    return 1\n\nX = 5\nY = 6\n"
        chunks = chunk_file("mod.py", content)
        gap = next(c for c in chunks if c.chunk_type == "fixed")
        assert gap.start_line == 3
        assert gap.end_line == 5


# ---------------------------------------------------------------------------
# Brace-less (expression-bodied) arrow functions
# ---------------------------------------------------------------------------


class TestExpressionBodiedArrows:
    def test_arrow_chunk_does_not_swallow_next_function(self):
        content = textwrap.dedent(
            """\
            export const add = (a, b) => a + b;
            export const sub = (a, b) => a - b;

            function big() {
              const x = 1;
              return x;
            }
            """
        )
        chunks = chunk_file("math.ts", content)
        by_name = {c.symbol_name: c for c in chunks}

        assert by_name["add"].start_line == 1
        assert by_name["add"].end_line == 1
        assert "big" not in by_name["add"].content
        assert by_name["sub"].end_line == 2
        assert "big" not in by_name["sub"].content
        assert "const x = 1;" in by_name["big"].content

    def test_braced_arrow_still_captures_its_body(self):
        content = textwrap.dedent(
            """\
            export const run = (a) => {
              return a * 2;
            };

            function other() {
              return 0;
            }
            """
        )
        chunks = chunk_file("run.ts", content)
        by_name = {c.symbol_name: c for c in chunks}
        assert "return a * 2;" in by_name["run"].content
        assert "other" not in by_name["run"].content
