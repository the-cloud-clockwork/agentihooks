from scripts.swarm.publish import LabRoute, lab_conclusion


class MissingRecord:
    def through_lab(self, route: LabRoute, head: str, url: str) -> str:
        # ruleid: lab-publish-polls-without-recording-head
        return lab_conclusion(url)


class RecordedPush:
    def through_lab(self, route: LabRoute, head: str, url: str) -> str:
        if route.pushed is not None:
            route.pushed(head, url)
        # ok: lab-publish-polls-without-recording-head
        return lab_conclusion(url)
