MEMBER = {
    "pub": {
        "allow": [
            "swarm.*.execution.>",
            "dead.*",
            "dead.*.*",
            "$JS.API.INFO",
            "$JS.API.STREAM.CREATE.*",
            "$JS.API.STREAM.MSG.GET.AGENTIHOOKS_DEAD",
            "$JS.API.CONSUMER.DURABLE.CREATE.*.*",
            "$JS.API.CONSUMER.INFO.>",
            "$JS.API.CONSUMER.MSG.NEXT.>",
            "$JS.ACK.>",
            "$KV.spoke-work.>",
            "$KV.spoke-pending.>",
            "$KV.hive-presence.>",
            "$KV.hive-executions.>",
            "$KV.execution-starts.>",
            "$JS.API.STREAM.MSG.GET.KV_spoke-work",
            "$JS.API.STREAM.MSG.GET.KV_spoke-pending",
            "$JS.API.STREAM.MSG.GET.KV_hive-presence",
            "$JS.API.STREAM.MSG.GET.KV_hive-executions",
            "$JS.API.STREAM.MSG.GET.KV_execution-starts",
        ]
    },
    "sub": {"allow": ["_INBOX.>"]},
}
