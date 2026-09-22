import csv
import io
import time


def benchmark_dict_reader(source):
    start = time.time()
    records = csv.DictReader(io.StringIO(source), strict=True)
    rows = []
    for raw in records:
        rows.append(raw)
    return time.time() - start


def benchmark_reader_custom_dict(source):
    start = time.time()
    records = csv.reader(io.StringIO(source), strict=True)
    fieldnames = next(records)

    fields = [
        "ticker", "model", "target_date", "origin_date", "ref_close",
        "pred_open", "open_p10", "open_p50", "open_p90",
        "pred_high", "high_p10", "high_p50", "high_p90",
        "pred_low", "low_p10", "low_p50", "low_p90",
        "pred_close", "close_p10", "close_p50", "close_p90"
    ]
    indices = {}
    for f in fields:
        try:
            indices[f] = fieldnames.index(f)
        except ValueError:
            pass

    rows = []
    for raw_row in records:
        raw = {f: raw_row[idx] if idx < len(raw_row) else None for f, idx in indices.items()}
        rows.append(raw)
    return time.time() - start


header = "ticker,model,origin_date,pred_close,pred_open,pred_high,pred_low,close_p10,close_p50,close_p90,open_p10,open_p50,open_p90,high_p10,high_p50,high_p90,low_p10,low_p50,low_p90,target_date,ref_close,extra1,extra2\n"
rows = "AAPL,model1,2023-01-01,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,2023-01-01,150.0,extra,extra\n" * 50000
source = header + rows

print(f"DictReader: {benchmark_dict_reader(source):.4f}s")
print(f"reader+custom_dict: {benchmark_reader_custom_dict(source):.4f}s")
