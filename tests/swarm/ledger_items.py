class LedgerItems:
    def item(self, slug, path):
        name, _, item_id = path.partition("/")
        return next((entry for entry in self.state(slug).get(name) or [] if entry.get("id") == item_id), None)
