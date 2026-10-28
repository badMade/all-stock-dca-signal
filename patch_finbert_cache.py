path = "ticker-advisor/signals.py"
with open(path, "r", encoding="utf-8") as f:
    code = f.read()

# Inject the import safely alongside existing standard library imports
if "import functools" not in code:
    code = code.replace("import math\n", "import functools\nimport math\n")

# Inject the decorator and Bolt documentation
target = "def _resolve_finbert_revision("
optimized = """# ⚡ Bolt Optimization: Cache HF Hub resolution to avoid repeated synchronous network calls
@functools.lru_cache(maxsize=8)
def _resolve_finbert_revision("""

if "@functools.lru_cache" not in code:
    code = code.replace(target, optimized)

with open(path, "w", encoding="utf-8") as f:
    f.write(code)

print("⚡ Patched ticker-advisor/signals.py with LRU cache.")
