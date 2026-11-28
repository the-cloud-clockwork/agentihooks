"""Lab heads stream, durable consumer and filter subject names, with no imports.

The protected NATS grant check reads LAB_STREAM and LAB_CONSUMER from this file by syntax tree without running it, so
both stay top level string literal assignments.
"""

LAB_STREAM = "AGENTIHOOKS-CI-LAB-HEADS"
LAB_SUBJECT = "ci.lab.head.>"
LAB_CONSUMER = "ci-lab-scheduler"
RELAY_CONSUMER = "ci-lab-relay"
RELAY_SUBJECT = "ci.lab.head.notice"
LAB_DURABLES = ((LAB_CONSUMER, LAB_SUBJECT), (RELAY_CONSUMER, RELAY_SUBJECT))
