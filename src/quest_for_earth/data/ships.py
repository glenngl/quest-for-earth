"""Ship classes and hull types — used by /api/ships endpoint."""

def get_ships():
    """Return the ship catalogue matching the API schema."""
    import json
    from pathlib import Path

    ships_path = Path(__file__).resolve().parent.parent / "data" / "ships.json"
    with open(ships_path, "r") as f:
        data = json.load(f)

    classes = data.get("classes", {})
    types = data.get("hull_types", {})

    return {
        "classes": classes,
        "types": types,
    }