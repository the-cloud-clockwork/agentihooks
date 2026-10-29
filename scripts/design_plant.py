class PlantedTools:
    @staticmethod
    def parse(text: str) -> int:
        return len(text)

    def run(self, n: int) -> int:
        return n


class PlantedHttp:
    def __init__(self) -> None:
        self.n = 1

    def planted_fetch(self, key: str) -> str:
        return key


class PlantedDisk:
    def __init__(self) -> None:
        self.n = 2

    def planted_fetch(self, key: str) -> str:
        return key
