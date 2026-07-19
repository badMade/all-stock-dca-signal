## 2025-01-20 - Fast JSON filtering
**Learning:** `rglob` followed by `json.loads` over a large number of historical artifact files is expensive. We can skip the expensive JSON parse by doing a fast substring check (`target_date_iso in raw_text`) before parsing.
**Action:** When scanning many JSON files for a specific value (like a specific trading day), read the file as a raw string and check for the substring before decoding it.
