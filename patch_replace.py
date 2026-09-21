import csv

def replace_in_file(path, search, replace):
    with open(path, "r") as f:
        content = f.read()
    content = content.replace(search, replace)
    with open(path, "w") as f:
        f.write(content)

search = """\
                    try:
                        header = next(reader)
                        idx_date = header.index("Date")
                        idx_close = header.index("Close")
                        idx_low = header.index("Low")
                    except (StopIteration, ValueError):
                        continue

                    prices = []
                    for row in reader:
                        # ⚡ Bolt Optimization: Use index-based access on csv.reader instead of csv.DictReader
                        # to avoid dictionary allocation overhead per row. Bound check avoids IndexError on short rows.
                        if len(row) > max(idx_date, idx_close, idx_low):
"""

replace = """\
                    try:
                        header = next(reader)
                        idx_date = header.index("Date")
                        idx_close = header.index("Close")
                        idx_low = header.index("Low")
                        max_idx = max(idx_date, idx_close, idx_low)
                    except (StopIteration, ValueError):
                        continue

                    prices = []
                    for row in reader:
                        # ⚡ Bolt Optimization: Use index-based access on csv.reader instead of csv.DictReader
                        # to avoid dictionary allocation overhead per row. Bound check avoids IndexError on short rows.
                        if len(row) > max_idx:
"""

replace_in_file("scripts/evaluate_deferral_params.py", search, replace)
