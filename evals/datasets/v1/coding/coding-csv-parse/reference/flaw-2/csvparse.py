def parse_csv(text: str) -> list[list[str]]:
    """Parse CSV text into records of fields."""
    records: list[list[str]] = []
    record: list[str] = []
    field: list[str] = []
    in_quotes = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_quotes:
            if ch == '"':
                in_quotes = False
            else:
                field.append(ch)
        elif ch == '"' and not field:
            in_quotes = True
        elif ch == ",":
            record.append("".join(field))
            field = []
        elif ch == "\n":
            record.append("".join(field))
            records.append(record)
            record, field = [], []
        elif ch != "\r":
            field.append(ch)
        i += 1
    if field or record:
        record.append("".join(field))
        records.append(record)
    return records
