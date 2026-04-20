---
description: Monitor tool — never ask, just act
alwaysApply: true
---

# Monitor Tool — Never Ask

## Anti-patterns (NEVER say these)
- "Would you like me to monitor this?"
- "Want me to set up monitoring?"
- "Should I watch for X?"
- "I can monitor the logs if you want."
- "Want me to monitor the CI build?"

If monitoring is the correct action, invoke the Monitor tool immediately. Monitoring is read-only and non-destructive. Full clearance covers it.

## When to Monitor (without asking)
- Build or CI logs during a deploy you triggered
- Rollout status after kubectl apply or helm upgrade
- Application logs when debugging
- Background processes you started
- Any wait where you need an outcome before proceeding

## When NOT to Monitor
- You already have the information
- The task is done with no follow-up observation needed
