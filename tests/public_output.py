class BufferedOutput:
    def __init__(self):
        self.pending = ""
        self.visible = ""

    def write(self, value):
        self.pending += value
        return len(value)

    def flush(self):
        self.visible += self.pending
        self.pending = ""
