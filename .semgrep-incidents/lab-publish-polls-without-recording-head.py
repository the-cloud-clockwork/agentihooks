from scripts.swarm.publish import LabRoute, lab_conclusion


def missing_record(route: LabRoute, head: str, url: str) -> str:
    def through_lab(route: LabRoute, head: str, url: str) -> str:
        # ruleid: lab-publish-polls-without-recording-head
        return lab_conclusion(url)

    return through_lab(route, head, url)


def recorded_push(route: LabRoute, head: str, url: str) -> str:
    def through_lab(route: LabRoute, head: str, url: str) -> str:
        if route.pushed is not None:
            route.pushed(head, url)
        # ok: lab-publish-polls-without-recording-head
        return lab_conclusion(url)

    return through_lab(route, head, url)
