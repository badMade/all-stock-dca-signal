import ast


def find_unused_imports(filepath):
    with open(filepath, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=filepath)
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported[(alias.asname or alias.name).split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported[alias.asname or alias.name] = node.lineno
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    return sorted(name for name in imported if name not in used)
