def patch_file():
    with open("scripts/generate_credit_usage_proxy_report.py", "r") as f:
        content = f.read()
    new_content = content.replace("""        if path.is_dir():
            total = 0
            import os
            stack = [path]
            while stack:
                current = stack.pop()
                try:
                    for entry in os.scandir(current):
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat().st_size
                except OSError:
                    pass
            return total""", """        if path.is_dir():
            total = 0
            import os
            stack = [path]
            while stack:
                current = stack.pop()
                try:
                    for entry in os.scandir(current):
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                total += entry.stat().st_size
                        except OSError:
                            pass
                except OSError:
                    pass
            return total""")
    new_content = new_content.replace("""        if path.is_dir():
            paths = []
            import os
            stack = [path]
            while stack:
                current = stack.pop()
                try:
                    for entry in os.scandir(current):
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            paths.append(entry.path.replace("\\\\", "/"))
                except OSError:
                    pass
            return sorted(paths)""", """        if path.is_dir():
            paths = []
            import os
            stack = [path]
            while stack:
                current = stack.pop()
                try:
                    for entry in os.scandir(current):
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                paths.append(entry.path.replace("\\\\", "/"))
                        except OSError:
                            pass
                except OSError:
                    pass
            return sorted(paths)""")
    with open("scripts/generate_credit_usage_proxy_report.py", "w") as f:
        f.write(new_content)
patch_file()
