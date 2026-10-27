class Hive:
    @property
    def auth(self):
        from scripts.hive import auth

        return auth

    @property
    def registry(self):
        from scripts.hive import registry

        return registry

    @property
    def launch(self):
        from scripts.hive import launch

        return launch

    @property
    def reports(self):
        from scripts.hive import reports

        return reports

    @property
    def headroom(self):
        from scripts.hive import headroom

        return headroom
