def parse_csv(text: str) -> list[list[str]]:
    """Parse CSV text into records of fields."""
    if not text:
        return []
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.split(",") for line in lines]
