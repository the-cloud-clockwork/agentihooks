from scripts.design_rules import Index


class PlantedIndex(Index):
    pass


def planted_untyped(path):
    return path


def planted_config(name: str, **options: object) -> None:
    return None
