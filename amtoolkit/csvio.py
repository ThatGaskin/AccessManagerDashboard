"""CSV import/export."""
import csv
import io

MAX_ROWS = 20000


class CsvError(ValueError):
    pass


def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvError("File is not UTF-8 or Windows-1252 text")


def parse(data: bytes, action):
    """Returns (rows, warnings) where rows is a list of (line_no, normalized_row)."""
    text = decode(data)
    if not text.strip():
        raise CsvError("The file is empty")
    # Pick the delimiter from the header line (Excel uses ';' in many locales). More predictable than csv.Sniffer.
    header_line = text.lstrip().splitlines()[0]
    delimiter = max((",", ";", "\t"), key=header_line.count)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    known = {action._norm(c) for c in action.column_names()}
    present = {action._norm(h) for h in headers}
    missing = [c.name for c in action.columns if c.required and action._norm(c.name) not in present]
    if missing:
        raise CsvError(f"Missing required column(s): {', '.join(missing)}. "
                       f"Expected columns: {', '.join(action.column_names())}")
    if not present & known:
        raise CsvError(f"None of the expected columns were found: {', '.join(action.column_names())}")
    warnings = [f"Column '{h}' is not used by this action and was ignored"
                for h in headers if action._norm(h) not in known]
    rows = []
    for record in reader:
        if None in record:
            warnings.append(f"Line {reader.line_num}: more values than columns; extra values ignored")
            record.pop(None)
        if not any((v or "").strip() for v in record.values()):
            continue
        rows.append((reader.line_num, action.normalize(record)))
        if len(rows) > MAX_ROWS:
            raise CsvError(f"More than {MAX_ROWS} rows; split the file into smaller jobs")
    if not rows:
        raise CsvError("No data rows found")
    return rows, warnings


def safe_cell(value) -> str:
    """Neutralises spreadsheet formula injection in exported files."""
    text = "" if value is None else str(value)
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


def write(headers, rows) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([safe_cell(h) for h in headers])
    for row in rows:
        writer.writerow([safe_cell(v) for v in row])
    return out.getvalue()


def template(action) -> str:
    return write(action.column_names(), [[c.example for c in action.columns]])
