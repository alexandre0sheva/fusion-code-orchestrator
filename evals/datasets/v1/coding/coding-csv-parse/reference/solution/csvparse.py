def parse_csv(text: str) -> list[list[str]]:
    """Parse CSV text into records of fields."""
    records: list[list[str]] = []
    record: list[str] = []
    field: list[str] = []
    i, n = 0, len(text)
    at_field_start = True
    while i < n:
        ch = text[i]
        if at_field_start and ch == '"':
            i += 1
            while True:
                if i >= n:
                    raise ValueError("unterminated quoted field")
                if text[i] == '"':
                    if i + 1 < n and text[i + 1] == '"':
                        field.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                field.append(text[i])
                i += 1
            if i < n and text[i] not in ",\r\n":
                raise ValueError("text after a closing quote")
            at_field_start = False
            continue
        if ch == ",":
            record.append("".join(field))
            field = []
            at_field_start = True
            i += 1
        elif ch == "\n" or (ch == "\r" and text[i + 1 : i + 2] == "\n"):
            record.append("".join(field))
            records.append(record)
            record, field = [], []
            at_field_start = True
            i += 2 if ch == "\r" else 1
        else:
            field.append(ch)
            at_field_start = False
            i += 1
    if field or record or not at_field_start or (text and not text.endswith("\n")):
        record.append("".join(field))
        records.append(record)
    return records
