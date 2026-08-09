import json
import math
import sys


def _fail_on_constant(s):
    raise json.JSONDecodeError(f"Invalid JSON constant: {s}", s, 0)


def _reject_duplicate_keys(pairs):
    seen = set()
    obj = {}
    for key, value in pairs:
        if key in seen:
            raise json.JSONDecodeError(f"Duplicate key: {key}", "", 0)
        seen.add(key)
        obj[key] = value
    return obj


def validate(filepath='testing_report.json'):
    """Validate a JSON test report and exit the process with the result.

    Reads ``filepath`` (resolved against the current working directory;
    defaults to ``testing_report.json``) and checks that it decodes to a
    JSON array of objects, each containing a ``suite`` string, a ``passed``
    bool, and a finite, non-negative, non-boolean numeric ``duration``.

    This function always calls ``sys.exit`` and never returns control to the
    caller: it exits 0 when every item passes validation, or 1 on the first
    missing file, unreadable path, JSON decode error, or field-level
    validation failure.
    """
    try:
        with open(filepath, 'rb') as f:
            data = json.load(f, parse_constant=_fail_on_constant, object_pairs_hook=_reject_duplicate_keys)
    except FileNotFoundError:
        print(f"Error: {filepath} not found")
        sys.exit(1)
    except OSError as e:
        print(f"Error: Unable to read {filepath}: {e}")
        sys.exit(1)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"Error: Invalid JSON format: {e}")
        sys.exit(1)

    if not isinstance(data, list):
        print("Error: Root element is not a JSON array")
        sys.exit(1)

    required_fields = {
        'suite': str,
        'passed': bool,
        'duration': (int, float)
    }

    for i, item in enumerate(data):
        if not isinstance(item, dict):
            print(f"Error: Item {i} is not a JSON object")
            sys.exit(1)

        for field, expected_type in required_fields.items():
            if field not in item:
                print(f"Error: Item {i} missing required field '{field}'")
                sys.exit(1)
            value = item[field]
            if type(value) is bool and expected_type is not bool:
                print(f"Error: Item {i} field '{field}' has wrong type")
                sys.exit(1)
            if not isinstance(value, expected_type):
                print(f"Error: Item {i} field '{field}' has wrong type")
                sys.exit(1)
            if field == 'duration':
                # 1e400 and similar overflow to float('inf') during JSON
                # decoding rather than raising, so isinstance(..., (int,
                # float)) alone would silently accept it as a valid duration.
                if isinstance(value, float) and not math.isfinite(value):
                    print(f"Error: Item {i} field '{field}' must be a finite non-negative number")
                    sys.exit(1)
                # Elapsed test durations cannot be negative; a negative value
                # indicates corrupted timing data rather than a real result.
                if value < 0:
                    print(f"Error: Item {i} field '{field}' must be a finite non-negative number")
                    sys.exit(1)

    print("Validation successful")
    sys.exit(0)


if __name__ == '__main__':
    target = sys.argv[1] if len(sys.argv) > 1 else 'testing_report.json'
    validate(target)
